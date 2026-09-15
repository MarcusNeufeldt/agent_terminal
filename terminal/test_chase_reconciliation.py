import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from chase import ChaseManager, ChaseWorker, ChaseTransient, ChaseUnknown


class ChaseReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.status = "FULLY_EXECUTED"
        self.filled = 461.0
        self.calls = []

        def get(path, **kwargs):
            self.calls.append(path)
            if path == "/openorders":
                return {"result": "success", "openOrders": []}
            raise TimeoutError("TLS handshake timed out")

        def post(path, **kwargs):
            self.calls.append(path)
            if path == "/cancelorder":
                return {"result": "success", "cancelStatus": {"status": "cancelled"}}
            self.assertEqual(path, "/orders/status", "No placement is permitted in reconciliation")
            return {"result": "success", "orders": [{"status": self.status, "order": {
                "orderId": "order-uni", "cliOrdId": "ch-uni-3", "filled": self.filled,
            }}]}

        self.ctx = SimpleNamespace(client=SimpleNamespace(get=get, post=post))
        self.spec = {"symbol": "PF_UNIUSD", "side": "sell", "size": 461.0, "reduceOnly": True}
        self.worker = ChaseWorker(self.spec, self.ctx, lambda *_: None)
        self.worker._active = {"cliOrdId": "ch-uni-3", "orderId": "order-uni", "size": 461.0,
                               "seenFilled": 0, "placedAt": 0}
        self.snapshot = {**self.worker.snapshot(), "status": "unknown", "audit": [
            {"event": "placement_intent", "params": {"cliOrdId": "ch-uni-3", "size": 461.0}},
        ]}

    def test_full_status_survives_unavailable_fills(self):
        self.assertTrue(self.worker._reconcile_resting())
        self.assertEqual(self.worker.filled, 461)
        self.assertIsNone(self.worker._active)
        self.assertNotIn("/fills", self.calls)

    def test_full_status_after_cancel_survives_unavailable_fills(self):
        self.worker._cancel_active()
        self.assertEqual(self.worker.filled, 461)
        self.assertIsNone(self.worker._active)
        self.assertNotIn("/fills", self.calls)

    def test_partial_status_retained_on_read_timeout_and_retried(self):
        self.status, self.filled = "ENTERED_BOOK", 29.1
        with self.assertRaises(ChaseTransient):
            self.worker._reconcile_resting()
        self.assertEqual(self.worker.filled, 29.1)
        self.assertIsNotNone(self.worker._active)
        self.status, self.filled = "FULLY_EXECUTED", 461.0
        self.assertTrue(self.worker._reconcile_resting())
        self.assertEqual(self.worker.filled, 461.0)
        self.assertNotIn("/sendorder", self.calls)

    def test_final_cancelled_quantity_does_not_depend_on_fills_endpoint(self):
        self.status, self.filled = "CANCELLED", 29.1
        self.worker._cancel_active()
        self.assertEqual(self.worker.filled, 29.1)
        self.assertIsNone(self.worker._active)
        self.assertNotIn("/fills", self.calls)

    def test_recovery_clears_only_freshly_confirmed_whole_peg(self):
        publish = Mock()
        manager = ChaseManager(publish)
        manager.recover([self.snapshot], [], self.ctx)
        self.assertEqual(manager.active(), [])
        result = manager.list()[0]
        self.assertEqual(result["id"], self.snapshot["id"])
        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["filled"], 461)
        self.assertIsNone(result["unknownReason"])
        self.assertTrue(all(call.args[1]["status"] == "filled" for call in publish.call_args_list))
        self.assertEqual(self.calls, ["/openorders", "/orders/status"])

    def test_recovery_preserves_unknown_if_reads_fail(self):
        self.status, self.filled = "ENTERED_BOOK", 29.1
        manager = ChaseManager(lambda *_: None)
        manager.recover([self.snapshot], [], self.ctx)
        self.assertEqual(manager.active()[0]["status"], "unknown")
        self.assertNotIn("/sendorder", self.calls)
        self.assertNotIn("/cancelorder", self.calls)

    def test_saved_full_execution_recovers_without_exchange_calls(self):
        self.snapshot["audit"].append({"event": "order_status", "orderId": "order-uni",
                                       "cliOrdId": "ch-uni-3", "status": "FULLY_EXECUTED", "filled": 461})
        manager = ChaseManager(lambda *_: None)
        manager.recover([self.snapshot], [], self.ctx)
        self.assertEqual(manager.active(), [])
        self.assertEqual(manager.list()[0]["filled"], 461)
        self.assertEqual(manager.list()[0]["status"], "filled")
        self.assertEqual(self.calls, [])

    def test_saved_evidence_requires_exact_identity_full_status_and_quantity(self):
        self.status, self.filled = "ENTERED_BOOK", 29.1
        evidence = {"event": "order_status", "orderId": "order-uni", "cliOrdId": "ch-uni-3",
                    "status": "FULLY_EXECUTED", "filled": 461}
        for change in [{"orderId": "other"}, {"cliOrdId": "other"}, {"filled": 460}, {"status": "ENTERED_BOOK"}]:
            with self.subTest(change=change):
                snapshot = {**self.snapshot, "audit": [*self.snapshot["audit"], {**evidence, **change}]}
                manager = ChaseManager(lambda *_: None)
                manager.recover([snapshot], [], self.ctx)
                self.assertEqual(manager.active()[0]["status"], "unknown")

    def test_still_open_exact_order_blocks_saved_completion(self):
        self.snapshot["audit"].append({"event": "order_status", "orderId": "order-uni",
                                       "status": "FULLY_EXECUTED", "filled": 461})
        manager = ChaseManager(lambda *_: None)
        manager.recover([self.snapshot], [{"order_id": "order-uni", "symbol": "PF_UNIUSD"}], self.ctx)
        self.assertEqual(manager.active()[0]["status"], "unknown")
        self.assertEqual(self.calls, [])

    def test_original_and_recovery_completion_survive_database_reload(self):
        import tempfile
        from pathlib import Path
        from db import Database
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            try:
                self.snapshot["audit"].append({"event": "order_status", "orderId": "order-uni",
                                               "status": "FULLY_EXECUTED", "filled": 461})
                copies = [self.snapshot, {**self.snapshot, "id": "recovery-" + self.snapshot["id"]}]
                for snapshot in copies:
                    database.log_event("chase", snapshot)
                manager = ChaseManager(database.log_event)
                manager.recover(database.latest_chase_snapshots(), [], self.ctx)
                self.assertEqual(manager.active(), [])
                saved = database.latest_chase_snapshots()
                self.assertEqual(len(saved), 2)
                self.assertTrue(all(s["status"] == "filled" and s["filled"] == 461 for s in saved))
                restored = ChaseManager(database.log_event)
                restored.recover(saved, [], self.ctx)
                self.assertEqual(restored.active(), [])
                self.assertEqual(self.calls, [])
            finally:
                database._conn.close()

    def test_recovery_does_not_guess_missing_or_partial_peg_history(self):
        for audit in [[], [{"event": "placement_intent", "params": {"cliOrdId": "ch-uni-3", "size": 400}}]]:
            manager = ChaseManager(lambda *_: None)
            manager.recover([{**self.snapshot, "audit": audit}], [], self.ctx)
            self.assertEqual(len(manager.active()), 1)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
