import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ai_chat
from db import Database
from grid import GridError
from grid_move import list_grids, move_grid


class GridMoveTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Database(Path(directory) / "test.db")
        self.addCleanup(self.db._conn.close)
        self.rows = {f"id-{i}": {"order_id": f"id-{i}", "symbol": "PF_TESTUSD", "side": "buy",
                     "limitPrice": price, "unfilledSize": 10, "orderType": "lmt", "reduceOnly": False}
                     for i, price in enumerate([10, 9, 8])}
        self.db.log_action("grid", True, [{"type": "ladder", "orders": 3, "size": 30}], [{
            "symbol": "PF_TESTUSD", "side": "buy", "responses": [
                {"outcome": "confirmed", "exchangeId": oid, "order": dict(order)} for oid, order in self.rows.items()]}])
        del self.rows["id-0"]  # first rung filled; never recreate it
        self.calls = []
        self.quote = {"last": 10, "bid": 10, "ask": 11}
        self.fail = None

        def get(path, **kwargs):
            self.assertEqual(path, "/openorders")
            return {"result": "success", "openOrders": [dict(r) for r in self.rows.values()]}

        def post(path, params, **kwargs):
            self.assertEqual(path, "/editorder")
            self.assertEqual(set(params), {"orderId", "limitPrice"}, "Amendment must not change quantity or submit a new order")
            self.calls.append(dict(params))
            if self.fail == "reject-second" and params["orderId"] == "id-2":
                return {"result": "success", "editStatus": {"status": "notFound"}}
            self.rows[params["orderId"]]["limitPrice"] = params["limitPrice"]
            if self.fail == "timeout":
                self.fail = None
                raise TimeoutError("response lost after edit")
            return {"result": "success", "editStatus": {"status": "edited"}}

        self.ctx = SimpleNamespace(client=SimpleNamespace(get=get, post=post),
            fresh_ticker=lambda _: self.quote, require_new_exposure=Mock(), instrument=lambda _: {"tickSize": 0.1})
        self.args = {"symbol": "PF_TESTUSD", "side": "buy"}

    def test_saved_identity_and_exact_amendments_preserve_remaining_grid(self):
        grids = list_grids(self.db, self.ctx, **self.args)
        self.assertEqual(grids[0]["originalOrders"], 3)
        self.assertEqual(grids[0]["workingOrders"], 2)
        result = move_grid(self.db, self.ctx, self.args, True)
        self.assertEqual(result["counts"]["moved"], 2)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual([r["limitPrice"] for r in self.rows.values()], [10, 9])
        self.assertEqual([r["unfilledSize"] for r in self.rows.values()], [10, 10])
        self.assertNotIn("id-0", self.rows)
        move_grid(self.db, self.ctx, {**self.args, "operationId": result["operationId"]}, True)
        self.assertEqual(len(self.calls), 2)

    def test_sell_grid_anchors_lowest_working_ask(self):
        for row in self.rows.values():
            row["side"] = "sell"
        self.db.log_action("grid", True, [{"type": "ladder"}], [{
            "symbol": "PF_TESTUSD", "side": "sell", "responses": [
                {"outcome": "confirmed", "exchangeId": oid, "order": dict(order)} for oid, order in self.rows.items()]}])
        self.quote = {"last": 7, "bid": 6.9, "ask": 7}
        result = move_grid(self.db, self.ctx, {**self.args, "side": "sell"}, True)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(sorted(r["limitPrice"] for r in self.rows.values()), [7, 8])

    def test_disarmed_is_read_only(self):
        result = move_grid(self.db, self.ctx, self.args, False)
        self.assertTrue(result["simulated"])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.db.grid_move(grid_id="1:0"))

    def test_crossing_price_fails_before_any_edit(self):
        self.quote["last"] = 12
        with self.assertRaisesRegex(GridError, "cross"):
            move_grid(self.db, self.ctx, self.args, True)
        self.assertEqual(self.calls, [])

    def test_missing_grid_does_not_rebuild_cancelled_orders(self):
        self.rows.clear()
        with self.assertRaises(GridError):
            move_grid(self.db, self.ctx, self.args, True)
        self.assertEqual(self.calls, [])

    def test_protection_is_never_moved(self):
        self.rows["id-1"]["reduceOnly"] = True
        with self.assertRaisesRegex(GridError, "Protection"):
            move_grid(self.db, self.ctx, self.args, True)
        self.assertEqual(self.calls, [])

    def test_timeout_after_acceptance_resumes_with_persisted_target(self):
        self.fail = "timeout"
        first = move_grid(self.db, self.ctx, self.args, True)
        self.assertEqual(first["outcome"], "partial")
        self.assertEqual(first["counts"]["unknown"], 1)
        self.assertEqual(len(self.calls), 1)
        self.quote["last"] = 10.5
        second = move_grid(self.db, self.ctx, {**self.args, "operationId": first["operationId"]}, True)
        self.assertEqual(second["outcome"], "confirmed")
        self.assertEqual(second["anchorPrice"], 10)
        self.assertEqual([r["limitPrice"] for r in self.rows.values()], [10, 9])
        self.assertEqual(len(self.calls), 2)

    def test_resume_from_another_database_connection_preserves_partial_fills(self):
        self.fail = "timeout"
        first = move_grid(self.db, self.ctx, self.args, True)
        self.rows["id-2"]["unfilledSize"] = 4
        path = self.db._conn.execute("PRAGMA database_list").fetchone()[2]
        reopened = Database(Path(path))
        self.addCleanup(reopened._conn.close)
        result = move_grid(reopened, self.ctx, {**self.args, "operationId": first["operationId"]}, True)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(self.rows["id-2"]["unfilledSize"], 4)
        self.assertEqual(len(self.calls), 2)

    def test_unrelated_order_overlap_rejected_before_edits(self):
        self.rows["manual"] = {**self.rows["id-1"], "order_id": "manual", "limitPrice": 10}
        with self.assertRaisesRegex(GridError, "overlaps"):
            move_grid(self.db, self.ctx, self.args, True)
        self.assertEqual(self.calls, [])

    def test_pending_rung_that_fills_is_not_recreated(self):
        self.fail = "timeout"
        result = move_grid(self.db, self.ctx, self.args, True)
        del self.rows["id-2"]
        result = move_grid(self.db, self.ctx, {**self.args, "operationId": result["operationId"]}, True)
        self.assertEqual(result["counts"]["not_working"], 1)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(len(self.calls), 1)

    def test_unknown_old_price_is_not_blindly_retried(self):
        self.fail = "timeout"
        first = move_grid(self.db, self.ctx, self.args, True)
        self.rows["id-1"]["limitPrice"] = 9
        second = move_grid(self.db, self.ctx, self.args, True)
        self.assertEqual(second["operationId"], first["operationId"])
        self.assertEqual(second["counts"]["unknown"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_known_rejection_resumes_without_repeating_completed_edits(self):
        self.fail = "reject-second"
        first = move_grid(self.db, self.ctx, self.args, True)
        self.assertEqual(first["counts"]["moved"], 1)
        self.assertEqual(first["counts"]["failed"], 1)
        self.fail = None
        second = move_grid(self.db, self.ctx, self.args, True)
        self.assertEqual(second["outcome"], "confirmed")
        self.assertEqual([r["orderId"] for r in self.calls], ["id-1", "id-2", "id-2"])

    def test_no_live_changes_when_current_orders_unavailable(self):
        self.ctx.client.get = lambda *a, **kw: {"result": "success"}
        with self.assertRaises(GridError):
            move_grid(self.db, self.ctx, self.args, True)
        self.assertEqual(self.calls, [])

    def test_wrong_operation_identity_is_rejected(self):
        self.fail = "timeout"
        result = move_grid(self.db, self.ctx, self.args, True)
        with self.assertRaises(GridError):
            move_grid(self.db, self.ctx, {**self.args, "side": "sell", "operationId": result["operationId"]}, True)
        self.assertEqual(len(self.calls), 1)


class ExecutionReceiptTests(unittest.TestCase):
    def test_side_scoped_bulk_cancel_is_exposed_and_preserves_opposite_tp(self):
        from actions import execute_actions
        tool = next(t["function"] for t in ai_chat.TOOLS if t["function"]["name"] == "cancel_all_for_symbol")
        self.assertEqual(tool["parameters"]["properties"]["side"]["enum"], ["buy", "sell"])
        orders = [{"order_id": oid, "symbol": symbol, "side": side} for oid, symbol, side in [
            ("zro-buy-1", "PF_ZROUSD", "buy"), ("zro-buy-2", "PF_ZROUSD", "buy"),
            ("zro-tp", "PF_ZROUSD", "sell"), ("ena-buy", "PF_ENAUSD", "buy"),
        ]]
        client = Mock()
        client.post.return_value = {"result": "success", "cancelStatus": {"status": "cancelled"}}
        ctx = SimpleNamespace(client=client, get_orders=lambda: orders, after_action=None)
        result = execute_actions([{"type": "cancel_all", "symbol": "PF_ZROUSD", "side": "buy"}], ctx, True)[0]
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual([call.kwargs["params"]["order_id"] for call in client.post.call_args_list], ["zro-buy-1", "zro-buy-2"])

    @patch.object(ai_chat, "_load_openrouter_key", return_value="fake")
    @patch.object(ai_chat, "_openrouter_completion")
    def test_model_failure_after_write_keeps_execution_receipt(self, completion, _key):
        completion.side_effect = [{"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "move", "function": {"name": "move_grid", "arguments": '{"symbol":"PF_ENAUSD"}'}},
        ]}}]}, RuntimeError("model connection failed")]
        executor = Mock(return_value={"outcome": "partial", "counts": {"moved": 3, "unknown": 1}, "operationId": "saved-move"})
        result = ai_chat.respond([], "", tool_executor=executor)
        for text in ["response failed", "3 moved", "1 unknown", "operationId=saved-move"]:
            self.assertIn(text, result["text"])


    @patch.object(ai_chat, "_load_openrouter_key", return_value="fake")
    @patch.object(ai_chat, "_openrouter_completion")
    def test_round_limit_reports_partial_cancellations_and_blocks_following_placement(self, completion, _key):
        def call(name, ident):
            return {"id": ident, "type": "function", "function": {"name": name,
                "arguments": json.dumps({"symbol": "PF_ENAUSD"})}}
        completion.return_value = {"choices": [{"message": {"content": "", "tool_calls": [
            call("cancel_all_for_symbol", "cancel"), call("place_ladder", "place"),
        ]}}]}
        executor = Mock(return_value={"outcome": "partial", "results": [
            *[{"outcome": "confirmed"} for _ in range(18)], {"outcome": "unknown", "error": "TLS timeout"},
        ]})
        result = ai_chat.respond([], "", tool_executor=executor, max_rounds=1)
        self.assertEqual(executor.call_count, 1)
        for text in ["tool-round limit", "18 confirmed", "1 unknown", "TLS timeout", "place_ladder PF_ENAUSD: blocked"]:
            self.assertIn(text, result["text"])


if __name__ == "__main__":
    unittest.main()
