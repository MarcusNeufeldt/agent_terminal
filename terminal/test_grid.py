import ast
import io
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlparse
from decimal import Decimal
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock

import actions
from actions import ActionError, execute_actions
from db import Database
from grid import GridError, build_grid_plan, validate_grid
from exchange_routing import ExchangeRouting, ExchangeRoutingError, requested_exchange


class GridTests(unittest.TestCase):
    def setUp(self):
        self.instrument = {"tickSize": 0.001, "contractValueTradePrecision": 1, "contractSize": 1}
        self.spec = {"type": "ladder", "symbol": "PF_UNIUSD", "side": "buy", "startPrice": 6.885,
                     "endPrice": 6.6, "size": 691.5, "orders": 10, "orderType": "post", "reduceOnly": False}
        self.ctx = SimpleNamespace(
            instrument=lambda _: self.instrument, fresh_ticker=lambda _: {"bid": 6.884, "ask": 6.89},
            get_positions=lambda: [], get_orders=lambda: [], require_new_exposure=Mock(),
            client=Mock(), after_action=None,
        )

    def test_exact_position_total_and_tick_aligned_range(self):
        plan = build_grid_plan(self.spec, self.instrument)
        self.assertEqual(len(plan["orders"]), 10)
        self.assertEqual(sum(Decimal(str(row["size"])) for row in plan["orders"]), Decimal("691.5"))
        self.assertEqual([plan["orders"][i]["limitPrice"] for i in [0, -1]], [6.885, 6.6])
        for row in plan["orders"]:
            self.assertEqual(Decimal(str(row["limitPrice"])) % Decimal("0.001"), 0)
        self.assertEqual(plan, build_grid_plan(self.spec, self.instrument))

    def test_usd_budget_never_exceeded(self):
        spec = {k: v for k, v in self.spec.items() if k != "size"}
        plan = build_grid_plan({**spec, "notional": 1000}, self.instrument)
        self.assertLessEqual(plan["notional"], 1000)
        self.assertGreater(plan["notional"], 990)

    def test_negative_size_precision_distributes_whole_lots(self):
        plan = build_grid_plan({**self.spec, "size": 1050, "orders": 3},
                               {**self.instrument, "contractValueTradePrecision": -2})
        self.assertEqual([r["size"] for r in plan["orders"]], [400, 300, 300])
        self.assertTrue(plan["warnings"])

    def test_invalid_inputs_fail_before_writes(self):
        for changes in [{"startPrice": "NaN"}, {"size": "Infinity"}, {"endPrice": 0},
                        {"orders": 2.5}, {"orders": 21}, {"orders": 1}, {"endPrice": 7},
                        {"notional": 20}, {"size": .1}, {"reduceOnly": "false"},
                        {"symbol": "PI_XBTUSD"}, {"orderType": "mkt"}]:
            with self.subTest(changes=changes), self.assertRaises(GridError):
                build_grid_plan({**self.spec, **changes}, self.instrument)

    def test_narrow_range_rejects_duplicate_ticks(self):
        with self.assertRaisesRegex(GridError, "too narrow"):
            build_grid_plan({**self.spec, "endPrice": 6.884}, self.instrument)

    def test_post_cross_rejects_and_limit_cross_warns(self):
        self.ctx.fresh_ticker = lambda _: {"bid": 6.7, "ask": 6.8}
        with self.assertRaisesRegex(GridError, "cross"):
            validate_grid(build_grid_plan(self.spec, self.instrument), self.ctx)
        warnings = validate_grid(build_grid_plan({**self.spec, "orderType": "lmt"}, self.instrument), self.ctx)
        self.assertIn("taker fees", warnings[0])

    def test_existing_order_overlap_blocks(self):
        self.ctx.get_orders = lambda: [{"symbol": "PF_UNIUSD", "side": "buy", "limitPrice": 6.6}]
        with self.assertRaisesRegex(GridError, "already exists"):
            validate_grid(build_grid_plan(self.spec, self.instrument), self.ctx)

    def test_unavailable_positions_or_orders_not_empty(self):
        for key in ["get_positions", "get_orders"]:
            original = getattr(self.ctx, key)
            setattr(self.ctx, key, lambda: [{"error": "offline"}])
            with self.assertRaisesRegex(GridError, "unavailable"):
                validate_grid(build_grid_plan(self.spec, self.instrument), self.ctx)
            setattr(self.ctx, key, original)

    def test_reduce_only_preserves_protection_and_caps_limit_exits(self):
        self.ctx.get_positions = lambda: [{"symbol": "PF_UNIUSD", "side": "long", "size": 691.5}]
        spec = {**self.spec, "side": "sell", "startPrice": 7, "endPrice": 7.2, "reduceOnly": True}
        stop = {"symbol": "PF_UNIUSD", "side": "sell", "orderType": "stp", "size": 691.5, "reduceOnly": True}
        self.ctx.get_orders = lambda: [stop]
        plan = build_grid_plan(spec, self.instrument)
        self.assertTrue(validate_grid(plan, self.ctx))
        self.ctx.require_new_exposure.assert_not_called()
        self.ctx.get_orders = lambda: [stop, {**stop, "orderType": "lmt", "limitPrice": 8, "size": 1}]
        with self.assertRaisesRegex(GridError, "exceeds"):
            validate_grid(plan, self.ctx)
        self.ctx.client.post.assert_not_called()

    def test_disarmed_grid_never_writes(self):
        result = execute_actions([self.spec], self.ctx, False)[0]
        self.assertEqual(result["outcome"], "simulated")
        self.assertEqual(len(result["orders"]), 10)
        self.ctx.client.post.assert_not_called()

    def test_changed_preview_rejected_before_first_order(self):
        plan = build_grid_plan(self.spec, self.instrument)
        action = {**self.spec, "size": 700, "previewHash": plan["previewHash"]}
        result = execute_actions([action], self.ctx, True)[0]
        self.assertEqual(result["outcome"], "rejected")
        self.ctx.client.post.assert_not_called()

    def test_http_grid_preview_and_submission_use_replay_and_arm_guards(self):
        # Load the handler only, not server startup threads, keys, or the live database.
        tree = ast.parse((Path(__file__).parent / "server.py").read_text())
        handler_ast = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TerminalHandler")
        imports = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        module = ast.fix_missing_locations(ast.Module(body=[imports, handler_ast], type_ignores=[]))
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            database = Database(Path(directory) / "test.db")
            cleanup.callback(database._conn.close)
            namespace = {"BaseHTTPRequestHandler": object, "json": json, "urlparse": urlparse,
                         "security": SimpleNamespace(validate_write=lambda _: None),
                         "IDEMPOTENT_WRITE_PATHS": {"/api/grid"}, "REQUEST_ID_RE": re.compile(r"^[A-Za-z0-9._:-]{8,100}$"),
                         "db": database, "arm_lock": threading.RLock(), "armed": False,
                         "exchange_routing": ExchangeRouting(threading.RLock()),
                         "ExchangeRoutingError": ExchangeRoutingError, "requested_exchange": requested_exchange,
                         "trading_actions": actions, "action_ctx": self.ctx, "cache": Mock()}
            exec(compile(module, "isolated_handler", "exec"), namespace)
            handler_type = namespace["TerminalHandler"]

            def post(path, body):
                handler = handler_type()
                raw = json.dumps(body).encode()
                handler.path = path
                handler.headers = {"Content-Length": str(len(raw))}
                handler.rfile = io.BytesIO(raw)
                handler.wfile = io.BytesIO()
                statuses = []
                handler.send_response = statuses.append
                handler.send_header = lambda *_: None
                handler.end_headers = lambda: None
                handler.do_POST()
                return statuses[-1], json.loads(handler.wfile.getvalue())

            status, preview = post("/api/grid/preview", self.spec)
            self.assertEqual(status, 200)
            self.assertTrue(preview["ready"])
            self.ctx.client.post.assert_not_called()
            self.assertEqual(database._conn.execute("select count(*) from write_requests").fetchone()[0], 0)
            body = {**self.spec, "previewHash": preview["plan"]["previewHash"],
                    "expectedArmed": False, "requestId": "grid-request-001"}
            first = post("/api/grid", body)
            self.assertEqual(first[1]["results"][0]["outcome"], "simulated")
            namespace["armed"] = True
            self.assertEqual(post("/api/grid", body), first)
            self.ctx.client.post.assert_not_called()
            self.assertEqual(database._conn.execute("select count(*) from actions_log").fetchone()[0], 1)
            self.assertEqual(post("/api/grid", {**body, "size": 700})[0], 409)
            self.assertEqual(post("/api/grid", {**body, "requestId": "grid-request-002"})[0], 409)
            self.ctx.client.post.assert_not_called()
            self.ctx.client.post.return_value = {"result": "success", "sendStatus": {"status": "placed", "order_id": "fake-order"}}
            live_body = {**body, "expectedArmed": True, "requestId": "grid-request-003"}
            live = post("/api/grid", live_body)
            self.assertEqual(live[1]["results"][0]["outcome"], "confirmed")
            self.assertEqual(self.ctx.client.post.call_count, 10)
            self.assertEqual(post("/api/grid", live_body), live)
            self.assertEqual(self.ctx.client.post.call_count, 10)

    def test_stale_account_mid_grid_preserves_partial_receipt(self):
        self.ctx.client.post.return_value = {"result": "success", "sendStatus": {"status": "placed", "order_id": "one"}}
        # Initial validation, dispatch guard, first rung, then stale account before rung two.
        self.ctx.require_new_exposure.side_effect = [None, None, None, ActionError("account unavailable")]
        result = execute_actions([self.spec], self.ctx, True)[0]
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(self.ctx.client.post.call_count, 1)
        self.assertIn("account unavailable", result["responses"][1]["error"])
        self.assertEqual(result["responses"][0]["exchangeId"], "one")

    def test_grid_stops_on_first_rejected_rung_and_logs_ids(self):
        self.ctx.client.post.side_effect = [
            {"result": "success", "sendStatus": {"status": "placed", "order_id": "one"}},
            {"result": "success", "sendStatus": {"status": "postWouldExecute"}},
        ]
        result = execute_actions([self.spec], self.ctx, True)[0]
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(self.ctx.client.post.call_count, 2)
        self.assertEqual(len(result["responses"]), 10)
        self.assertEqual(result["responses"][0]["exchangeId"], "one")
        self.assertIn("not executed", result["responses"][2]["error"])


if __name__ == "__main__":
    unittest.main()
