import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chase import ChaseManager, ChaseWorker, ChaseTransient, ChaseUnknown
from db import Database


class ExternalCancellationTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.get.return_value = {"result": "success", "openOrders": []}
        self.spec = {"symbol": "PF_TESTUSD", "side": "sell", "size": 10, "reduceOnly": True}
        self.worker = ChaseWorker(self.spec, SimpleNamespace(client=self.client), lambda *_: None)
        self.worker._active = {"orderId": "order", "cliOrdId": "ch-test-1", "size": 10,
                               "seenFilled": 0, "placedAt": 0, "price": Decimal(1)}
        self.worker._instrument = lambda: {"tickSize": 0.1, "contractValueTradePrecision": 0}
        self.worker._wait = Mock()

    def status(self, filled=0):
        return {"result": "success", "orders": [{"status": "CANCELLED", "order": {
            "orderId": "order", "cliOrdId": "ch-test-1", "filled": filled}}]}

    def test_run_stops_on_external_cancel_with_no_exchange_writes(self):
        self.client.post.return_value = self.status()
        self.worker.run()
        self.assertEqual(self.worker.status, "cancelled")
        self.assertEqual(self.worker.filled, 0)
        self.assertIsNone(self.worker._active)
        self.assertIsNone(self.worker.unknown_reason)
        self.assertEqual([c.args[0] for c in self.client.post.call_args_list], ["/orders/status"])

    def test_partial_cancel_preserves_fills_from_all_pegs(self):
        self.worker._base_filled = 3
        self.worker._active.update(size=7, seenFilled=1)
        self.client.post.return_value = self.status(2)
        self.worker.run()
        self.assertEqual(self.worker.status, "partial")
        self.assertEqual(self.worker.filled, 5)
        self.assertEqual(self.worker.stop_reason, "externally_cancelled")
        self.assertEqual([c.args[0] for c in self.client.post.call_args_list], ["/orders/status"])

    def test_missing_or_contradictory_cancel_quantity_stays_unknown(self):
        for quantity in [None, -1, 11, "NaN", "Infinity"]:
            with self.subTest(quantity=quantity):
                self.client.post.return_value = self.status(quantity)
                with self.assertRaises(ChaseUnknown):
                    self.worker._reconcile_resting()
                self.assertIsNotNone(self.worker._active)
        self.worker._active["seenFilled"] = 2
        self.client.post.return_value = self.status(1)
        with self.assertRaises(ChaseUnknown):
            self.worker._reconcile_resting()

    def test_contradictory_client_identity_stays_unknown(self):
        response = self.status()
        response["orders"][0]["order"]["cliOrdId"] = "another-order"
        self.client.post.return_value = response
        with self.assertRaises(ChaseUnknown):
            self.worker._reconcile_resting()

    def test_malformed_open_order_snapshot_cannot_prove_absence(self):
        self.client.get.return_value = {"result": "success", "openOrders": [None]}
        with self.assertRaises(ChaseTransient):
            self.worker._reconcile_resting()
        self.client.post.assert_not_called()

    def test_external_cancel_uses_history_after_status_expires(self):
        self.client.post.return_value = {"result": "success", "orders": []}
        history = {"elements": [{"uid": "event", "event": {"OrderCancelled": {
            "reason": "would_not_reduce_position", "order": {"uid": "order", "clientId": "ch-test-1",
            "tradeable": "PF_TESTUSD", "direction": "Sell", "quantity": "10", "filled": "0"}}}}]}
        with patch("account_log._get", return_value=history):
            self.worker.run()
        self.assertEqual(self.worker.status, "cancelled")
        self.assertEqual(self.worker.stop_reason, "would_not_reduce_position")
        self.assertEqual([c.args[0] for c in self.client.post.call_args_list], ["/orders/status"])

    def test_saved_external_cancel_and_alias_remain_resolved_after_restart(self):
        snapshot = {**self.worker.snapshot(), "id": "original", "status": "unknown", "audit": [
            {"event": "placement_intent", "params": {"cliOrdId": "ch-test-1", "size": 10}},
            {"event": "order_status", "orderId": "order", "cliOrdId": "ch-test-1",
             "status": "CANCELLED", "filled": 0},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            try:
                database.log_event("chase", snapshot)
                database.log_event("chase", {**snapshot, "id": "recovery-original"})
                manager = ChaseManager(database.log_event)
                manager.recover(database.latest_chase_snapshots(), [], self.worker.ctx)
                self.assertEqual(manager.active(), [])
                saved = database.latest_chase_snapshots()
                self.assertEqual({s["id"] for s in saved}, {"original", "recovery-original"})
                self.assertTrue(all(s["status"] == "cancelled" and s["filled"] == 0 for s in saved))
                restored = ChaseManager(database.log_event)
                restored.recover(saved, [], self.worker.ctx)
                self.assertEqual(restored.active(), [])
                self.client.get.assert_not_called()
                self.client.post.assert_not_called()
            finally:
                database._conn.close()

    def test_restart_can_recover_a_fresh_external_cancel_without_cancel_receipt(self):
        snapshot = {**self.worker.snapshot(), "status": "unknown", "audit": [
            {"event": "placement_intent", "params": {"cliOrdId": "ch-test-1", "size": 10}},
        ]}
        self.client.post.return_value = self.status(3)
        manager = ChaseManager(lambda *_: None)
        manager.recover([snapshot], [], self.worker.ctx)
        self.assertEqual(manager.active(), [])
        self.assertEqual(manager.list()[0]["status"], "partial")
        self.assertEqual(manager.list()[0]["filled"], 3)
        self.assertEqual([c.args[0] for c in self.client.post.call_args_list], ["/orders/status"])

    def test_no_position_or_wrong_side_prevents_reduce_only_placement(self):
        for positions in [[], [{"symbol": "PF_TESTUSD", "side": "short", "size": 10}]]:
            with self.subTest(positions=positions):
                worker = ChaseWorker(self.spec, self.worker.ctx, lambda *_: None)
                self.client.get.return_value = {"result": "success", "openPositions": positions}
                worker._place(Decimal(1), Decimal(10))
                self.assertEqual(worker.status, "cancelled")
                self.assertEqual(worker.stop_reason, "no_reducible_position")
                self.assertIsNone(worker._active)
                self.client.post.assert_not_called()

    def test_unavailable_positions_do_not_look_flat(self):
        for response in [{"result": "error"}, {"result": "success", "openPositions": [None]},
                         {"result": "success", "openPositions": [{}]}]:
            self.client.get.return_value = response
            with self.assertRaises(ChaseTransient):
                self.worker._place(Decimal(1), Decimal(10))
            self.client.post.assert_not_called()

    def test_every_replacement_is_capped_to_fresh_exposure(self):
        worker = ChaseWorker(self.spec, self.worker.ctx, lambda *_: None)
        worker._instrument = self.worker._instrument
        worker._peg_price = Mock(side_effect=[1, 2])
        worker._wait = Mock()
        sent, live = [], []

        def get(path, **kwargs):
            if path == "/openpositions":
                return {"result": "success", "openPositions": [{"symbol": "PF_TESTUSD", "side": "long",
                                                                 "size": 10 if not sent else 4}]}
            return {"result": "success", "openOrders": live.copy(), "fills": []}

        def post(path, params, **kwargs):
            if path == "/sendorder":
                sent.append(params)
                if len(sent) == 1:
                    live.append({"order_id": "first", "unfilledSize": 10})
                return {"result": "success", "sendStatus": {"status": "placed", "order_id": "first" if len(sent) == 1 else "second"}}
            if path == "/cancelorder":
                live.clear()
                return {"result": "success", "cancelStatus": {"status": "cancelled"}}
            self.assertEqual(path, "/orders/status")
            return {"result": "success", "orders": [{"status": "CANCELLED" if len(sent) == 1 else "FULLY_EXECUTED",
                    "order": {"orderId": worker._active["orderId"], "filled": 3 if len(sent) == 1 else 4}}]}

        self.client.get.side_effect = get
        self.client.post.side_effect = post
        worker.run()
        self.assertEqual([s["size"] for s in sent], [10, 4])
        self.assertTrue(all(s["reduceOnly"] for s in sent))
        self.assertEqual(worker.filled, 7)
        self.assertEqual(worker.status, "partial")


if __name__ == "__main__":
    unittest.main()
