import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from actions import ActionError
from db import Database
from tp_cleanup import TakeProfitCleanup
from chase import ChaseManager


class TakeProfitCleanupTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.db = Database(Path(directory) / "test.db")
        self.addCleanup(self.db._conn.close)
        self.positions = [{"symbol": "PF_ENAUSD", "side": "long", "size": 10}]
        self.orders = [
            {"order_id": "tp", "symbol": "PF_ENAUSD", "side": "sell", "orderType": "take_profit", "reduceOnly": True, "unfilledSize": 10},
            {"order_id": "entry", "symbol": "PF_ENAUSD", "side": "buy", "orderType": "lmt", "reduceOnly": False, "unfilledSize": 10},
            {"order_id": "sl", "symbol": "PF_ENAUSD", "side": "sell", "orderType": "stp", "reduceOnly": True, "unfilledSize": 10},
            {"order_id": "other", "symbol": "PF_UNIUSD", "side": "buy", "orderType": "lmt", "unfilledSize": 10},
        ]
        self.status, self.filled = "FULLY_EXECUTED", 10
        self.cancelled = []
        self.cancel_fails = False
        self.reopen = False
        self.chase = SimpleNamespace(active=lambda: [], abort_all=Mock(return_value={"pending": []}))

        def post(path, params, **kwargs):
            if path == "/orders/status":
                return {"result": "success", "orders": [{"status": self.status, "order": {"orderId": "tp", "filled": self.filled}}]}
            self.assertEqual(path, "/cancelorder", "Cleanup must never place or close a position")
            if self.cancel_fails:
                raise TimeoutError("cancel timeout")
            self.cancelled.append(params["order_id"])
            self.orders = [o for o in self.orders if o["order_id"] != params["order_id"]]
            if self.reopen:
                self.positions = [{"symbol": "PF_ENAUSD", "side": "long", "size": 1}]
            return {"result": "success", "cancelStatus": {"status": "cancelled"}}

        self.ctx = SimpleNamespace(client=SimpleNamespace(post=post), get_positions=lambda: self.positions,
                                   get_orders=lambda: self.orders)
        self.publish = Mock()
        self.cleanup = TakeProfitCleanup(self.db, self.ctx, self.chase, lambda: None, self.publish)
        self.cleanup.step(True)
        self.assertEqual(self.cancelled, [])

    def hit_tp(self):
        self.positions = []
        self.orders = [o for o in self.orders if o["order_id"] != "tp"]

    def test_confirmed_full_tp_cancels_pair_only_and_is_not_repeated(self):
        self.hit_tp()
        self.cleanup.step(True)
        self.assertEqual(self.cancelled, ["entry", "sl"])
        self.assertEqual([o["order_id"] for o in self.orders], ["other"])
        self.assertEqual(self.db.tp_cleanup_states()[0]["status"], "completed")
        self.cleanup.step(True)
        self.assertEqual(len(self.cancelled), 2)

    def test_disarmed_defers_cleanup_and_recovers_from_database(self):
        self.hit_tp()
        self.cleanup.step(False)
        self.assertEqual(self.cancelled, [])
        self.assertEqual(self.db.tp_cleanup_states()[0]["status"], "pending")
        restored = TakeProfitCleanup(self.db, self.ctx, self.chase, lambda: None, self.publish)
        restored.step(True)
        self.assertEqual(self.cancelled, ["entry", "sl"])

    def test_cancelled_tp_or_partial_execution_does_not_trigger(self):
        for status, filled in [("CANCELLED", 0), ("FULLY_EXECUTED", 5)]:
            self.status, self.filled = status, filled
            self.hit_tp()
            self.cleanup.step(True)
            self.assertEqual(self.cancelled, [])

    def test_tp_fill_between_positions_and_orders_reads_is_not_lost(self):
        self.orders = [o for o in self.orders if o["order_id"] != "tp"]
        self.cleanup.step(True)  # positions read was just before execution
        self.assertEqual(self.db.tp_cleanup_states()[0]["status"], "watching")
        self.positions = []
        self.cleanup.step(True)
        self.assertEqual(self.cancelled, ["entry", "sl"])

    def test_old_missing_tp_is_not_carried_into_a_later_manual_close(self):
        self.orders = [o for o in self.orders if o["order_id"] != "tp"]
        self.cleanup.step(True)
        self.cleanup.step(True)
        self.positions = []
        self.cleanup.step(True)
        self.assertEqual(self.cancelled, [])

    def test_position_still_open_does_not_trigger(self):
        self.cleanup.step(True)
        self.assertEqual(self.cancelled, [])
        self.chase.abort_all.assert_not_called()

    def test_unavailable_positions_fail_closed(self):
        self.positions = [{"error": "offline"}]
        with self.assertRaises(ActionError):
            self.cleanup.step(True)
        self.assertEqual(self.cancelled, [])

    def test_unknown_cancellation_stops_then_retries_exact_id(self):
        self.hit_tp()
        self.cancel_fails = True
        self.cleanup.step(True)
        self.assertEqual(self.db.tp_cleanup_states()[0]["results"], {"entry": {"outcome": "unknown", "error": "TimeoutError: cancel timeout"}})
        self.cancel_fails = False
        self.cleanup.step(True)
        self.assertEqual(self.cancelled, ["entry", "sl"])

    def test_reopened_position_pauses_remaining_cleanup(self):
        self.hit_tp()
        self.reopen = True
        self.cleanup.step(True)
        self.assertEqual(self.cancelled, ["entry"])
        self.assertEqual(self.db.tp_cleanup_states()[0]["status"], "paused")
        self.assertTrue(any(o["order_id"] == "sl" for o in self.orders))

    def test_orders_added_after_disarmed_capture_are_not_cancelled(self):
        self.hit_tp()
        self.cleanup.step(False)
        self.orders.append({"order_id": "new-intent", "symbol": "PF_ENAUSD", "side": "buy", "orderType": "lmt"})
        self.cleanup.step(True)
        self.assertNotIn("new-intent", self.cancelled)

    def test_chase_stop_is_scoped_and_unconfirmed_stop_blocks_cancellation(self):
        self.chase.active = lambda: [{"id": "ena-worker", "symbol": "PF_ENAUSD"}, {"id": "uni-worker", "symbol": "PF_UNIUSD"}]
        self.chase.abort_all.return_value = {"pending": [{"id": "ena-worker"}]}
        self.hit_tp()
        self.cleanup.step(True)
        self.chase.abort_all.assert_called_with(chase_ids={"ena-worker"})
        self.assertEqual(self.cancelled, [])

    def test_chase_manager_does_not_abort_other_symbols_workers(self):
        manager = ChaseManager(lambda *_: None)
        workers = [Mock(id=name, status="running") for name in ["ena", "uni"]]
        for worker in workers:
            worker.is_alive.side_effect = [True, False, False]
        manager._chases = {worker.id: worker for worker in workers}
        manager.abort_all(chase_ids={"ena"})
        workers[0].abort.assert_called_once()
        workers[1].abort.assert_not_called()


if __name__ == "__main__":
    unittest.main()
