"""Venue-boundary tests. AST-load HTTP handlers, never server startup or live clients."""

import ast
import io
import json
import queue
import re
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

from exchange_routing import ExchangeRouting, ExchangeRoutingError, requested_exchange
import hyperliquid_trading
import hyperliquid_recovery
import hyperliquid_fills
import hyperliquid_lifecycle
import hyperliquid_grid
from grid import GridError
from hyperliquid_backend import HyperliquidBackend, READ_ONLY_MESSAGE
from read_state import account_payload
from local_security import LocalSecurity


class ExchangeRoutingTests(unittest.TestCase):
    def setUp(self):
        lock = threading.RLock()
        self.routing = ExchangeRouting(lock)
        self.kraken = Mock()
        self.db = Mock()
        self.db.claim_write_request.return_value = {"state": "new", "result": {}, "status": 200}
        self.hl_client = Mock(host="unused.test")
        self.hl = HyperliquidBackend(Mock(), client=self.hl_client, account_address="", enable_feed=False)
        self.chases = Mock()
        self.chases.active.return_value = []
        self.hl_write = Mock(side_effect=lambda path, body, **_: {"exchange": "hyperliquid", "type": "order",
                                                            "outcome": "simulated", "live": False,
                                                            "action": {"type": "order"}, "rows": []})
        self.account = Mock(return_value={"balanceValue": 123})
        self.scanner = Mock()
        self.scanner.scan_volatility_hyperliquid.return_value = {"rows": [], "exchange": "hyperliquid"}
        source = ast.parse(Path(__file__).with_name("server.py").read_text())
        handler = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "TerminalHandler")
        future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
        module = ast.fix_missing_locations(ast.Module(body=[future, handler], type_ignores=[]))
        self.ns = {"BaseHTTPRequestHandler": object, "json": json, "queue": queue, "urlparse": urlparse, "parse_qs": parse_qs,
                   "security": SimpleNamespace(validate_write=lambda _: None, valid_host=lambda _: True,
                                                valid_token=lambda _: True, token="fixture-token",
                                                consume_arm_challenge=lambda _: True,
                                                issue_arm_challenge=lambda: "fixture-challenge"),
                   "scanner": self.scanner, "exchange_routing": self.routing, "ExchangeRoutingError": ExchangeRoutingError,
                   "requested_exchange": requested_exchange,
                   "REQUEST_ID_RE": re.compile(r"^[A-Za-z0-9._:-]{8,100}$"), "READ_ONLY_MESSAGE": READ_ONLY_MESSAGE,
                   "arm_lock": lock, "armed": True, "client": self.kraken, "db": self.db,
                   "sse": Mock(), "hyperliquid_sse": Mock(), "hyperliquid": self.hl, "chase_manager": self.chases,
                   "get_account": self.account, "_account_payload": account_payload,
                   "hyperliquid_write": self.hl_write, "hyperliquid_trading": hyperliquid_trading,
                   "hyperliquid_order_action": Mock(return_value={"type": "order", "orders": []}),
                   "hyperliquid_leverage_intent": Mock(return_value={"type": "leverageIntent", "action": {"type": "updateLeverage"}, "expiresAfter": 1}),
                   "trading_actions": SimpleNamespace(GridError=GridError), "hyperliquid_recovery": hyperliquid_recovery, "hyperliquid_fills": hyperliquid_fills,
                   "hyperliquid_lifecycle": hyperliquid_lifecycle, "hyperliquid_grid": hyperliquid_grid,
                   "hyperliquid_gate": Mock(return_value={"mode": "off", "reason": "disabled"}),
                   "IDEMPOTENT_WRITE_PATHS": {"/api/order", "/api/leverage", "/api/chart-order", "/api/cancel", "/api/action", "/api/grid",
                                              "/api/flatten", "/api/chase", "/api/chase/abort", "/api/chat"},
                   "HL_WRITE_PATHS": {"/api/order", "/api/leverage", "/api/chart-order", "/api/cancel", "/api/order-reconcile", "/api/cancel-reconcile", "/api/fill-history/sync", "/api/grid/preview", "/api/grid"},
                   "VENUE_NEUTRAL_PATHS": {"/api/arm", "/api/arm/challenge"}}
        exec(compile(module, "isolated_exchange_handler", "exec"), self.ns)
        self.handler = self.ns["TerminalHandler"]

    def request(self, path, body=None, epoch=None, headers=None):
        handler = self.handler()
        raw = json.dumps(body if body is not None else {}).encode()
        handler.path = path
        handler.headers = {"Content-Length": str(len(raw)), **(headers or {})}
        if epoch is not None:
            handler.headers["X-Terminal-Exchange-Epoch"] = str(epoch)
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *_: None
        handler.end_headers = lambda: None
        (handler.do_GET if body is None else handler.do_POST)()
        return statuses[-1], json.loads(handler.wfile.getvalue())

    def switch(self, target, source="kraken", epoch=0):
        return self.request(f"/api/exchange?exchange={source}", {"exchange": target}, epoch)

    def test_default_kraken_and_explicit_hyperliquid_reads_never_fall_through(self):
        status, account = self.request("/api/account")
        self.assertEqual((status, account["balanceValue"]), (200, 123))
        self.hl_client.info.assert_not_called()
        status, selected = self.switch("hyperliquid")
        self.assertEqual(status, 200)
        self.assertFalse(selected["armed"])
        self.assertFalse(self.ns["armed"])
        self.chases.abort_all.assert_not_called()
        status, account = self.request("/api/account?exchange=hyperliquid")
        self.assertEqual((status, account["state"]), (200, "unavailable"))
        self.assertNotIn("balanceValue", account)
        self.assertEqual(self.account.call_count, 1, "HL never queries Kraken account or its DB")
        self.assertEqual(self.request("/api/account")[0], 409)
        self.assertEqual(self.account.call_count, 1)
        self.kraken.assert_not_called()

    def test_venue_list_reports_the_signed_trading_gate(self):
        status, data = self.request("/api/exchanges")
        self.assertEqual(status, 200)
        venues = {row["id"]: row for row in data["venues"]}
        self.assertFalse(venues["kraken"]["readOnly"])
        self.assertTrue(venues["hyperliquid"]["readOnly"], "the browser has no Hyperliquid trading UI")
        self.assertEqual(venues["hyperliquid"]["signedTrading"], "off")
        self.assertNotIn("signer", venues["hyperliquid"], "never publish key-derived identity from discovery")

    def test_arming_works_from_the_hyperliquid_venue(self):
        # ARM is one process-wide flag, so it must not be gated on the active venue.
        self.switch("hyperliquid")
        status, data = self.request("/api/arm/challenge?exchange=hyperliquid")
        self.assertEqual(status, 200, "the arming challenge must not answer 501 on Hyperliquid")
        self.assertTrue(data["challenge"])

        status, data = self.request("/api/arm?exchange=hyperliquid",
                                    {"armed": True, "challenge": "fixture-challenge"}, 1)
        self.assertEqual(status, 200)
        self.assertTrue(data["armed"])
        self.assertTrue(self.ns["armed"], "arming on Hyperliquid arms the same global gate")
        self.ns["hyperliquid_sse"].publish.assert_any_call("armed", {"armed": True})
        status, health = self.request("/api/health?exchange=hyperliquid")
        self.assertEqual(status, 200)
        self.assertTrue(health["armed"],
                        "the venue health must report the live ARM state, never a constant")

        status, data = self.request("/api/arm?exchange=hyperliquid", {"armed": False}, 1)
        self.assertEqual(status, 200)
        self.assertFalse(data["armed"])
        self.assertFalse(self.ns["armed"])
        self.assertFalse(self.request("/api/health?exchange=hyperliquid")[1]["armed"])
        self.hl_write.assert_not_called()

    def test_hyperliquid_writes_leave_kraken_untouched_and_unsupported_paths_are_refused(self):
        self.switch("hyperliquid")
        for path in ("action", "flatten", "chase",
                     "chase/abort", "chat", "chat/note", "chat/reset", "anything-new"):
            with self.subTest(path=path):
                status, data = self.request(f"/api/{path}?exchange=hyperliquid", {"armed": True}, 1)
                self.assertEqual(status, 405)
                self.assertEqual(data["outcome"], "rejected")
        self.hl_write.assert_not_called()
        self.db.claim_write_request.assert_not_called()
        for path in ("order", "cancel"):
            status, data = self.request(f"/api/{path}?exchange=hyperliquid",
                                        {"requestId": f"fixture-request-{path}", "symbol": "HL_APT"}, 1)
            self.assertEqual(status, 200)
            self.assertEqual(data["exchange"], "hyperliquid")
        self.assertEqual(self.hl_write.call_count, 2)
        self.assertEqual(self.db.claim_write_request.call_count, 2)
        for call in self.db.claim_write_request.call_args_list:
            identity = call.args[2]
            self.assertEqual(identity["venue"], "hyperliquid")
            self.assertEqual(identity["network"], self.ns["hyperliquid"].network)
            self.assertEqual(identity["account"], self.ns["hyperliquid"].account_address.lower())
            self.assertEqual(identity["body"]["symbol"], "HL_APT")
        self.account.assert_not_called()
        self.kraken.post.assert_not_called()
        self.hl_client.info.assert_not_called()

    def test_recovery_route_reads_exchange_and_persists_evidence_without_signing(self):
        self.switch("hyperliquid")
        self.hl.account_configured = True
        self.hl.account_address = "0x" + "1" * 40
        cloid = "0x" + "a" * 32
        self.db.venue_write_request.return_value = {"symbol": "HL_APT", "cloid": cloid}
        self.hl.order_status = Mock(return_value={"found": True, "symbol": "HL_APT", "cliOrdId": cloid,
                                                 "order_id": "123", "orderStatus": "open"})
        status, result = self.request("/api/order-reconcile?exchange=hyperliquid", {"requestId": "request-original"}, 1)
        self.assertEqual(status, 200)
        self.assertEqual(result["outcome"], "reconciled")
        self.db.save_write_reconciliation.assert_called_once()
        self.db.claim_write_request.assert_not_called()
        self.hl_write.assert_not_called()
        self.kraken.post.assert_not_called()
        self.assertFalse(self.ns["armed"])
        self.db.venue_unresolved.return_value = {"items": [], "hasMore": False}
        status, result = self.request("/api/execution-recovery?exchange=hyperliquid")
        self.assertEqual((status, result["state"]), (200, "current"))

    def test_cancel_recovery_routes_read_only_the_saved_target(self):
        self.switch("hyperliquid")
        self.hl.account_configured = True
        self.db.venue_cancellations.return_value = {"items": [{"requestId": "cancel-original",
            "body": {"symbol": "HL_APT", "orderIds": ["123"]}, "result": {"outcome": "unknown"}}], "hasMore": False}
        self.hl.order_status = Mock(return_value={"found": True, "symbol": "HL_APT", "order_id": "123", "orderStatus": "canceled"})
        status, result = self.request("/api/cancel-recovery?exchange=hyperliquid")
        self.assertEqual((status, result["items"][0]["targets"]), (200, ["123"]))
        status, result = self.request("/api/cancel-reconcile?exchange=hyperliquid", {"requestId": "cancel-original", "target": "123"}, 1)
        self.assertEqual((status, result["state"], result["canReplace"]), (200, "observed", False))
        self.hl.order_status.assert_called_once_with("123")
        self.db.save_write_reconciliation.assert_called_once()
        self.db.claim_write_request.assert_not_called()
        self.hl_write.assert_not_called()
        self.kraken.post.assert_not_called()

    def test_hyperliquid_grid_preview_checks_are_read_only_and_never_sign(self):
        self.switch("hyperliquid")
        self.hl.markets = Mock(return_value={"HL_APT": {"instrument": {
            "symbol": "HL_APT", "tradeable": True, "contractValueTradePrecision": 2}}})
        body = {"symbol": "HL_APT", "side": "buy", "startPrice": 0.6, "endPrice": 0.5, "orders": 3, "notional": 100}
        status, result = self.request("/api/grid/preview?exchange=hyperliquid", body, 1)
        self.assertEqual(status, 200)
        self.assertFalse(result["previewOnly"])
        self.assertEqual(len(result["plan"]["orders"]), 3)
        self.hl.orderbook = Mock(return_value={"time": time.time() * 1000, "orderBook": {"bids": [[0.5, 1]], "asks": [[0.7, 1]]}})
        self.hl.positions = Mock(return_value={"positions": []})
        self.hl.orders = Mock(return_value={"orders": []})
        status, checked = self.request("/api/grid/preview?exchange=hyperliquid", {**body, "checkCurrentOrders": True}, 1)
        self.assertEqual(status, 200)
        self.assertTrue(checked["orderChecksPassed"])
        self.hl.orders.assert_called_once_with(fresh=True)
        # Placement exists now, but a preview body alone is not a submission: it
        # carries no request identity and no per-rung client IDs, so nothing is
        # journalled and nothing is signed.
        status, refused = self.request("/api/grid?exchange=hyperliquid", body, 1)
        self.assertEqual(status, 400)
        self.db.prepare_hyperliquid_order.assert_not_called()
        self.hl_write.assert_not_called()
        self.db.claim_write_request.assert_not_called()
        self.kraken.post.assert_not_called()
        self.hl_client.info.assert_not_called()

    def test_chart_route_checks_epoch_and_persists_before_dispatch_without_kraken_fallback(self):
        self.switch("hyperliquid")
        prepared = {"type": "chartIntent", "action": {"type": "batchModify", "modifies": []}, "expiresAfter": 1}
        prepare = Mock(return_value=prepared)
        self.ns["hyperliquid_chart"] = SimpleNamespace(prepare=prepare)
        body = {"requestId": "chart-fixture", "symbol": "HL_APT", "kind": "sl"}
        status, _ = self.request("/api/chart-order?exchange=hyperliquid", body, 0)
        self.assertEqual(status, 409)
        prepare.assert_not_called()
        def dispatch(path, received, *, prepared_action):
            self.db.prepare_hyperliquid_order.assert_called_once_with(body["requestId"], prepared)
            self.assertEqual(path, "/api/chart-order")
            return {"type": "batchModify", "action": prepared_action["action"], "outcome": "simulated", "live": False}
        self.hl_write.side_effect = dispatch
        status, result = self.request("/api/chart-order?exchange=hyperliquid", body, 1)
        self.assertEqual((status, result["outcome"]), (200, "simulated"))
        self.kraken.post.assert_not_called()
        self.assertEqual(self.hl_write.call_count, 1)

    def test_leverage_route_journals_prepared_intent_before_dispatch(self):
        self.switch("hyperliquid")
        body = {"symbol": "HL_APT", "leverage": 5, "expectedLeverage": 3, "cross": True,
                "expectedArmed": False, "requestId": "leverage-fixture"}
        def dispatch(path, received, *, prepared_action):
            self.db.prepare_hyperliquid_order.assert_called_once_with(body["requestId"], prepared_action)
            self.assertEqual(path, "/api/leverage")
            return {"exchange": "hyperliquid", "type": "updateLeverage", "outcome": "simulated",
                    "action": prepared_action["action"], "live": False}
        self.hl_write.side_effect = dispatch
        status, result = self.request("/api/leverage?exchange=hyperliquid", body, 1)
        self.assertEqual((status, result["outcome"]), (200, "simulated"))
        self.kraken.post.assert_not_called()
        self.assertEqual(self.hl_write.call_count, 1)

    def test_grid_route_journals_the_prepared_batch_before_dispatch(self):
        self.switch("hyperliquid")
        cloids = ["0x" + f"{n:032x}" for n in (1, 2, 3)]
        body = {"symbol": "HL_APT", "cloids": cloids, "previewHash": "hash-fixture",
                "expectedArmed": False, "requestId": "grid-fixture"}
        prepared = {"type": "order", "grouping": "na",
                    "orders": [{"a": 1, "b": True, "p": "1", "s": "1", "r": False, "c": c} for c in cloids]}
        self.ns["hyperliquid_grid"] = Mock()
        self.ns["hyperliquid_grid"].prepare.return_value = prepared

        def dispatch(path, received, *, prepared_action):
            # The exact signed batch must be journalled before anything is sent.
            self.db.prepare_hyperliquid_order.assert_called_once_with(body["requestId"], prepared)
            self.assertEqual(path, "/api/grid")
            self.assertIs(prepared_action, prepared)
            return {"exchange": "hyperliquid", "type": "order", "outcome": "simulated", "simulated": True,
                    "live": False, "action": prepared_action, "rows": []}
        self.hl_write.side_effect = dispatch
        status, result = self.request("/api/grid?exchange=hyperliquid", body, 1)
        self.assertEqual((status, result["outcome"]), (200, "simulated"))
        self.ns["hyperliquid_grid"].prepare.assert_called_once_with(body, self.hl, cloids)
        # The browser matches this against its saved batch receipt before trusting it.
        row = result["results"][0]
        self.assertEqual((row["batch"], row["cloids"], row["requestId"]), (True, cloids, "grid-fixture"))
        self.assertEqual(row["responses"], [])
        self.assertEqual(row["outcome"], "simulated")
        self.kraken.post.assert_not_called()

    def test_grid_planning_failures_are_rejections_not_crashes(self):
        self.switch("hyperliquid")
        self.ns["hyperliquid_grid"] = Mock()
        self.ns["hyperliquid_grid"].prepare.side_effect = GridError("Grid preview changed.")
        body = {"symbol": "HL_APT", "cloids": ["0x" + f"{n:032x}" for n in (1, 2)],
                "expectedArmed": False, "requestId": "grid-reject"}
        status, result = self.request("/api/grid?exchange=hyperliquid", body, 1)
        self.assertEqual(status, 400)
        self.assertEqual(result["outcome"], "rejected")
        self.assertIn("Grid preview changed", result["error"])
        self.db.prepare_hyperliquid_order.assert_not_called()
        self.hl_write.assert_not_called()

    def test_batch_recovery_handler_persists_progress_without_signing(self):
        from test_hyperliquid_batch_recovery import BatchRecoveryTests
        batch = BatchRecoveryTests()
        batch.setUp()
        try:
            self.switch("hyperliquid")
            self.ns["db"] = batch.db
            self.ns["hyperliquid"] = batch.backend
            for remaining in (1, 0):
                status, result = self.request("/api/order-reconcile?exchange=hyperliquid", {"requestId": "batch-original"}, 1)
                self.assertEqual((status, result["remaining"]), (200, remaining))
            self.assertEqual(batch.backend.order_status.call_count, 2)
            self.hl_write.assert_not_called()
            self.kraken.post.assert_not_called()
        finally:
            batch.tearDown()

    def test_fill_history_routes_never_sign_or_touch_kraken(self):
        self.switch("hyperliquid")
        self.hl.account_configured = True
        self.hl.account_address = "0x" + "1" * 40
        self.hl_client.info.return_value = []
        self.db.save_hyperliquid_fill_page.return_value = 0
        self.db.hyperliquid_order_fills.return_value = []
        status, result = self.request("/api/fill-history/sync?exchange=hyperliquid", {"startTime": 1, "endTime": 2}, 1)
        self.assertEqual(status, 200)
        self.assertTrue(result["scanComplete"])
        self.assertFalse(result["historyComplete"])
        self.hl_client.info.assert_called_once_with("userFillsByTime", user=self.hl.account_address,
                                                  startTime=1, endTime=2, aggregateByTime=False)
        status, result = self.request("/api/fill-history?exchange=hyperliquid&orderId=123")
        self.assertEqual(status, 200)
        self.assertEqual(result["observedFilledSize"], "0")
        self.assertFalse(result["historyComplete"])
        self.db.claim_write_request.assert_not_called()
        self.hl_write.assert_not_called()
        self.kraken.post.assert_not_called()
        self.assertFalse(self.ns["armed"])

    def test_lifecycle_route_is_read_only_and_keeps_unknown_unresolved(self):
        self.switch("hyperliquid")
        self.hl.account_configured = True
        self.hl.account_address = "0x" + "1" * 40
        self.db.venue_write_request.return_value = {"symbol": "HL_APT", "cloid": "0x" + "a" * 32}
        self.hl.order_status = Mock(return_value={"found": False, "uncertain": True, "orderStatus": "unknownOid"})
        status, result = self.request("/api/order-lifecycle?exchange=hyperliquid&requestId=request-original")
        self.assertEqual(status, 200)
        self.assertEqual(result["automationDecision"], "wait")
        self.assertFalse(result["canReplace"])
        self.db.save_write_reconciliation.assert_not_called()
        self.db.claim_write_request.assert_not_called()
        self.hl_write.assert_not_called()
        self.kraken.post.assert_not_called()

    def test_prepared_order_is_saved_before_the_exact_action_is_submitted(self):
        from test_hyperliquid_trading import WriteDecisionTests
        writes = WriteDecisionTests()
        writes.setUp()
        self.ns["hyperliquid_order_action"] = writes.ns["hyperliquid_order_action"]
        self.ns["hyperliquid_write"] = writes.ns["hyperliquid_write"]
        self.switch("hyperliquid")
        self.ns["armed"] = writes.ns["armed"] = True
        events = []
        self.db.prepare_hyperliquid_order.side_effect = lambda *_: events.append("prepared")
        submit = writes.trader.submit
        def recorded_submit(action, count=1):
            events.append("submitted")
            return submit(action, count)
        writes.trader.submit = recorded_submit
        body = {**writes.order(), "requestId": "prepared-order-fixture", "cloid": "0x" + "a" * 32}
        status, result = self.request("/api/order?exchange=hyperliquid", body, 1)
        self.assertEqual(status, 200)
        self.assertEqual(events, ["prepared", "submitted"])
        prepared = self.db.prepare_hyperliquid_order.call_args.args[1]
        self.assertEqual(prepared["orders"][0]["s"], "2.99")
        self.assertEqual(writes.trader.calls[0][0], prepared)
        self.db.prepare_hyperliquid_order.side_effect = ValueError("fixture storage failure")
        status, result = self.request("/api/order?exchange=hyperliquid", {**body, "requestId": "prepared-order-failure"}, 1)
        self.assertEqual(status, 400)
        self.assertEqual(len(writes.trader.calls), 1, "storage failure must prevent another submission")
        self.kraken.post.assert_not_called()

    def test_bulk_cancel_handler_maps_real_adapter_results_to_exact_ids(self):
        from test_hyperliquid_trading import WriteDecisionTests, make_trader
        writes = WriteDecisionTests()
        writes.setUp()
        self.ns["hyperliquid_write"] = writes.ns["hyperliquid_write"]
        self.switch("hyperliquid")
        self.ns["armed"] = writes.ns["armed"] = True
        ids = ["12345678901234567890", "2"]
        for index, (statuses, expected) in enumerate([
                (["success", {"error": "already filled"}], ["confirmed", "rejected"]),
                (["success"], ["unknown", "unknown"]),
                (["success", "success"], ["confirmed", "confirmed"])]):
            trader = make_trader(response={"status": "ok", "response": {"type": "cancel", "data": {"statuses": statuses}}})
            writes.ns["_hl_trader"]["trader"] = trader
            status, result = self.request("/api/cancel?exchange=hyperliquid", {
                "symbol": "HL_APT", "orderIds": ids, "requestId": f"bulk-cancel-fixture-{index}"}, 1)
            self.assertEqual(status, 200)
            self.assertEqual([row["orderId"] for row in result["cancelResults"]], ids)
            self.assertEqual([row["outcome"] for row in result["cancelResults"]], expected)
            for row in result["cancelResults"]:
                if row["outcome"] == "confirmed":
                    self.assertIsNone(row["error"], "another target's rejection must not contaminate a success")
            self.assertEqual(len(trader.transport.payloads), 1, "no blind retry")
            self.assertEqual(trader.transport.payloads[0]["action"]["cancels"], [{"a": 1, "o": int(ids[0])}, {"a": 1, "o": 2}])
        self.ns["armed"] = writes.ns["armed"] = False
        _, result = self.request("/api/cancel?exchange=hyperliquid", {
            "symbol": "HL_APT", "orderIds": ids, "requestId": "bulk-cancel-simulation"}, 1)
        self.assertEqual([row["outcome"] for row in result["cancelResults"]], ["simulated", "simulated"])
        self.assertEqual(len(trader.transport.payloads), 1)
        self.kraken.post.assert_not_called()

    def test_account_cancel_is_one_real_adapter_batch_with_per_target_results(self):
        from test_hyperliquid_trading import WriteDecisionTests, make_trader, MARKETS
        writes = WriteDecisionTests()
        writes.setUp()
        original = writes.ns["hyperliquid"].markets()
        writes.ns["hyperliquid"].markets = lambda: {**original, "HL_BTC": {"instrument": {**MARKETS["HL_BTC"], "tradeable": True}}}
        trader = make_trader(response={"status": "ok", "response": {"type": "cancel", "data": {
            "statuses": ["success", {"error": "already filled"}]}}})
        writes.ns["_hl_trader"]["trader"] = trader
        self.ns["hyperliquid_write"] = writes.ns["hyperliquid_write"]
        self.switch("hyperliquid")
        self.ns["armed"] = writes.ns["armed"] = True
        targets = [{"symbol": "HL_APT", "orderId": "12345678901234567890"}, {"symbol": "HL_BTC", "orderId": "2"}]
        status, result = self.request("/api/cancel?exchange=hyperliquid", {"targets": targets, "requestId": "account-cancel-test"}, 1)
        self.assertEqual((status, result["outcome"]), (200, "partial"))
        self.assertEqual([row["outcome"] for row in result["cancelResults"]], ["confirmed", "rejected"])
        self.assertEqual(self.db.claim_write_request.call_args.args[2]["body"]["targets"], targets)
        self.assertEqual(len(trader.transport.payloads), 1)
        self.assertEqual(trader.transport.payloads[0]["action"]["cancels"], [{"a": 1, "o": 12345678901234567890}, {"a": 0, "o": 2}])
        self.kraken.post.assert_not_called()

    def test_browser_cancel_payload_reaches_real_hyperliquid_validation(self):
        from test_hyperliquid_trading import WriteDecisionTests
        writes = WriteDecisionTests()
        writes.setUp()
        self.ns["hyperliquid_write"] = writes.ns["hyperliquid_write"]
        self.switch("hyperliquid")
        oid = "12345678901234567890"
        for index, target in enumerate(({"orderId": oid}, {"cliOrdId": "0x" + "a" * 32})):
            status, result = self.request("/api/cancel?exchange=hyperliquid", {
                "symbol": "HL_APT", "requestId": f"cancel-wire-fixture-{index}", **target}, 1)
            self.assertEqual(status, 200)
            self.assertEqual(result["outcome"], "simulated")
            cancel = result["action"]["cancels"][0]
            if "orderId" in target:
                self.assertEqual(cancel, {"a": 1, "o": int(oid)})
            else:
                self.assertEqual(cancel, {"asset": 1, "cloid": target["cliOrdId"]})
        self.assertEqual(writes.trader.calls, [])
        self.kraken.post.assert_not_called()

    def test_unsupported_hyperliquid_reads_do_not_leak_kraken_history_or_alerts(self):
        self.switch("hyperliquid")
        for path in ("chat/history", "chase", "tp-cleanup", "protection/alerts", "stats", "equity", "alt-btc", "signal"):
            status, data = self.request(f"/api/{path}?exchange=hyperliquid")
            self.assertEqual((status, data["state"]), (501, "unsupported"))
        self.db.get_messages.assert_not_called()
        self.db.get_equity.assert_not_called()
        self.hl_client.info.assert_not_called()
        self.scanner.scan_volatility.assert_not_called()

    def test_volatility_scan_reads_the_hyperliquid_universe_not_the_kraken_one(self):
        self.switch("hyperliquid")
        status, data = self.request("/api/volatility?exchange=hyperliquid&limit=7&minVolume=250000")
        self.assertEqual(status, 200)
        self.assertEqual(data["exchange"], "hyperliquid")
        self.scanner.scan_volatility.assert_not_called()
        called = self.scanner.scan_volatility_hyperliquid.call_args
        self.assertIs(called.args[0], self.hl, "the scan must read the Hyperliquid backend")
        self.assertEqual(called.kwargs["limit"], 7)
        self.assertEqual(called.kwargs["min_volume_quote"], 250000)
        self.kraken.get.assert_not_called()

    def test_invalid_or_duplicate_venue_is_not_defaulted_to_kraken(self):
        for query in ("exchange=unknown", "exchange=", "exchange=kraken&exchange=hyperliquid"):
            self.assertEqual(self.request("/api/account?" + query)[0], 400)
            self.assertEqual(self.request("/api/order?" + query, {}, 0)[0], 400)
        self.account.assert_not_called()
        self.db.claim_write_request.assert_not_called()

    def test_switch_back_still_rejects_old_epoch_and_legacy_writes(self):
        self.switch("hyperliquid")
        self.assertEqual(self.switch("kraken", "hyperliquid", 1)[0], 200)
        for epoch in (0, 1, None):
            self.assertEqual(self.request("/api/arm?exchange=kraken", {"armed": True}, epoch)[0], 409)
        self.assertFalse(self.ns["armed"])
        self.assertEqual(self.request("/api/session")[1]["exchangeEpoch"], 2)

    def test_inflight_request_blocks_switch_without_disarming(self):
        with self.routing.request("kraken", "0"):
            self.assertEqual(self.switch("hyperliquid")[0], 409)
            self.assertEqual(self.routing.active, "kraken")
            self.assertTrue(self.ns["armed"])
        self.assertEqual(self.switch("hyperliquid")[0], 200)

    def test_chase_or_unknown_worker_blocks_switch_without_cancelling_it(self):
        for state in ("running", "unknown", "orphaned"):
            self.chases.active.return_value = [{"id": "fixture", "status": state}]
            self.assertEqual(self.switch("hyperliquid")[0], 409)
            self.assertTrue(self.ns["armed"])
        self.chases.abort_all.assert_not_called()

    def test_body_cannot_silently_override_venue(self):
        status, _ = self.request("/api/order", {"exchange": "hyperliquid", "symbol": "PF_APTUSD"}, 0)
        self.assertEqual(status, 400)
        self.db.claim_write_request.assert_not_called()

    def test_failed_chase_read_is_not_an_empty_worker_list(self):
        self.chases.active.return_value = None
        self.assertEqual(self.switch("hyperliquid")[0], 503)
        self.chases.active.side_effect = RuntimeError("fixture unavailable")
        self.assertEqual(self.switch("hyperliquid")[0], 503)
        self.assertTrue(self.ns["armed"])
        self.assertEqual(self.routing.active, "kraken")

    def test_real_local_security_also_guards_the_switch(self):
        self.ns["security"] = security = LocalSecurity(9999)
        self.assertEqual(self.switch("hyperliquid")[0], 403)
        self.assertTrue(self.ns["armed"])
        headers = {"Host": "127.0.0.1:9999", "Origin": "http://127.0.0.1:9999",
                   "Content-Type": "application/json", "X-Terminal-Token": security.token}
        status, _ = self.request("/api/exchange?exchange=kraken", {"exchange": "hyperliquid"}, 0, headers)
        self.assertEqual(status, 200)
        self.assertFalse(self.ns["armed"])

    def test_hyperliquid_sse_uses_only_its_bus_and_stops_on_switch(self):
        self.switch("hyperliquid")
        q = Mock()
        def next_event(**_):
            self.routing.switch("hyperliquid", "1", "kraken", active_chases=lambda: [], disarm=lambda: None)
            return "ticker", {"symbol": "HL_APT", "exchange": "hyperliquid", "last": .6}
        q.get.side_effect = next_event
        self.ns["hyperliquid_sse"].subscribe.return_value = q
        handler = self.handler()
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler._stream_sse("hyperliquid")
        data = handler.wfile.getvalue().decode()
        self.assertIn('"symbol": "HL_APT"', data)
        self.assertIn("event: exchange", data)
        self.ns["sse"].subscribe.assert_not_called()
        self.chases.list.assert_not_called()
        self.ns["hyperliquid_sse"].unsubscribe.assert_called_once_with(q)

    def test_same_venue_is_noop_and_reports_actual_arm_state(self):
        status, data = self.switch("kraken")
        self.assertEqual(status, 200)
        self.assertTrue(data["armed"])
        self.assertTrue(self.ns["armed"])
        self.assertEqual(self.routing.epoch, 0)


if __name__ == "__main__":
    unittest.main()
