import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from chase import ChaseManager, ChaseWorker, ChaseUnknown


class CancellationRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.ctx = SimpleNamespace(client=self.client)
        self.spec = {"symbol": "PF_ZROUSD", "side": "sell", "size": 10, "reduceOnly": True}
        self.worker = ChaseWorker(self.spec, self.ctx, lambda *_: None)
        self.worker._active = {"orderId": "order", "cliOrdId": "ch-test-1", "size": 10,
                               "seenFilled": 0, "placedAt": 0}
        self.client.get.side_effect = lambda path, **kw: {"result": "success", "openOrders": [], "fills": []}
        self.cancel = {"result": "success", "cancelStatus": {"status": "cancelled"}}
        self.status = {"result": "success", "orders": [{"status": "CANCELLED", "order": {
            "orderId": "order", "cliOrdId": "ch-test-1", "filled": 0}}]}
        self.sleep = self.enterContext(patch("chase.time.sleep"))

    def history(self, filled="0"):
        return {"elements": [{"uid": "event-id", "event": {"OrderCancelled": {"order": {
            "uid": "order", "clientId": "ch-test-1", "tradeable": "PF_ZROUSD", "direction": "Sell",
            "quantity": "10", "filled": filled}}}}]}

    def test_status_timeout_retries_reads_without_repeating_cancel(self):
        self.client.post.side_effect = [self.cancel, TimeoutError("TLS timeout"), self.status]
        self.worker._cancel_active()
        self.assertIsNone(self.worker._active)
        self.assertEqual([c.args[0] for c in self.client.post.call_args_list],
                         ["/cancelorder", "/orders/status", "/orders/status"])
        self.sleep.assert_called_once_with(1.0)

    def test_exhausted_reads_remain_unknown_and_keep_identity(self):
        self.client.post.side_effect = [self.cancel] + [TimeoutError("offline")] * 3
        with self.assertRaises(ChaseUnknown):
            self.worker._cancel_active()
        self.assertEqual(self.worker._active["orderId"], "order")
        self.assertTrue(self.worker._active["cancelConfirmed"])
        self.assertEqual(sum(c.args[0] == "/cancelorder" for c in self.client.post.call_args_list), 1)

    def test_worker_never_replaces_before_reconciliation_succeeds(self):
        for permanent in [False, True]:
            with self.subTest(permanent=permanent):
                worker = ChaseWorker({**self.spec, "maxRepegs": 2}, self.ctx, lambda *_: None)
                worker._instrument = lambda: {"tickSize": 0.1, "contractValueTradePrecision": 0}
                worker._peg_price = Mock(side_effect=[1, 2])
                worker._wait = Mock()
                sent, status_reads, live = [], [], []

                def get(path, **kwargs):
                    return {"result": "success", "openOrders": live.copy(), "fills": [],
                            "openPositions": [{"symbol": "PF_ZROUSD", "side": "long", "size": 10}]}

                def post(path, **kwargs):
                    if path == "/sendorder":
                        if sent:
                            self.assertTrue(any(a["event"] == "cancellation_reconciled" for a in worker.audit))
                        oid = f"peg-{len(sent) + 1}"
                        sent.append(oid)
                        live.append({"order_id": oid, "cliOrdId": worker._active["cliOrdId"], "unfilledSize": 10})
                        return {"result": "success", "sendStatus": {"status": "placed", "order_id": oid}}
                    if path == "/cancelorder":
                        live.clear()
                        return self.cancel
                    self.assertEqual(path, "/orders/status")
                    status_reads.append(path)
                    if permanent or len(status_reads) == 1:
                        raise TimeoutError("TLS timeout")
                    return {"result": "success", "orders": [{"status": "CANCELLED", "order": {
                        "orderId": worker._active["orderId"], "filled": 0}}]}

                self.client.get.side_effect = get
                self.client.post.side_effect = post
                worker.run()
                self.assertEqual(len(sent), 1 if permanent else 2)
                self.assertEqual(worker.status, "unknown" if permanent else "max_repegs")

    def test_cancellation_write_timeout_is_not_retried(self):
        self.client.post.side_effect = TimeoutError("write uncertain")
        with self.assertRaises(ChaseUnknown):
            self.worker._cancel_active()
        self.assertEqual(self.client.post.call_count, 1)
        self.client.get.assert_not_called()

    def test_expired_status_uses_paginated_exact_cancelled_history(self):
        self.client.post.side_effect = [self.cancel, {"result": "success", "orders": []}]
        with patch("account_log._get", side_effect=[{"elements": [], "continuationToken": "next"}, self.history("3")]) as history:
            self.worker._cancel_active()
        self.assertEqual(history.call_count, 2)
        self.assertIn("continuation_token=next", history.call_args.args[2])
        self.assertEqual(self.worker.filled, 3)
        self.assertIsNone(self.worker._active)
        self.assertFalse(any(c.args[0] == "/fills" for c in self.client.get.call_args_list))

    def test_contradictory_history_stays_blocked(self):
        for field, value in [("clientId", "other"), ("quantity", "11"), ("filled", "NaN"), ("tradeable", "PF_UNIUSD")]:
            with self.subTest(field=field):
                response = self.history()
                response["elements"][0]["event"]["OrderCancelled"]["order"][field] = value
                self.worker._active["cancelConfirmed"] = True
                self.client.post.return_value = {"result": "success", "orders": []}
                with patch("account_log._get", return_value=response), self.assertRaises(ChaseUnknown):
                    self.worker._retry_cancel_reconciliation()
                self.assertIsNotNone(self.worker._active)

    def test_startup_recovers_cancelled_worker_without_writes_or_restart_of_trade(self):
        snapshot = {**self.worker.snapshot(), "status": "unknown", "audit": [
            {"event": "placement_intent", "params": {"cliOrdId": "ch-test-1", "size": 10}},
            {"event": "cancellation_result", "cliOrdId": "ch-test-1", "orderId": "order", "nestedStatus": "cancelled"},
        ]}
        self.client.post.return_value = {"result": "success", "orders": []}
        published = Mock()
        manager = ChaseManager(published)
        with patch("account_log._get", return_value=self.history()):
            manager.recover([snapshot], [], self.ctx)
        self.assertEqual(manager.active(), [])
        self.assertEqual(manager.list()[0]["status"], "cancelled")
        self.assertEqual(manager.list()[0]["filled"], 0)
        self.assertTrue(all(c.args[0] == "/orders/status" for c in self.client.post.call_args_list))
        self.assertEqual(published.call_args.args[1]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
