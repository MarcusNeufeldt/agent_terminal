import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from db import Database
from hyperliquid_client import HyperliquidError
from hyperliquid_recovery import reconcile, unresolved, cancellations, reconcile_cancel


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.db = Database(self.path)
        self.account = "0x" + "1" * 40
        self.cloid = "0x" + "a" * 32
        self.payload = {"venue": "hyperliquid", "network": "mainnet", "account": self.account,
                        "body": {"symbol": "HL_APT", "cloid": self.cloid, "size": 20}}
        self.backend = SimpleNamespace(network="mainnet", account_address=self.account,
            account_configured=True, order_status=Mock(return_value={"found": True,
                "symbol": "HL_APT", "cliOrdId": self.cloid, "order_id": "12345678901234567890",
                "orderStatus": "open"}))

    def tearDown(self):
        self.db._conn.close()
        self.temp.cleanup()

    def claim(self, identifier="request-first", payload=None):
        return self.db.claim_write_request(identifier, "/api/order", payload or self.payload)

    def test_cancel_readbacks_survive_restart_without_rewriting_original_result(self):
        ids = ["12345678901234567890", "12345678901234567891"]
        payload = {**self.payload, "body": {"symbol": "HL_APT", "orderIds": ids}}
        self.db.claim_write_request("cancel-original", "/api/cancel", payload)
        original = {"outcome": "unknown", "error": "response lost"}
        self.db.complete_write_request("cancel-original", 200, original)
        first = reconcile_cancel(self.db, self.backend, "cancel-original", ids[0])
        self.assertFalse(first["canReplace"])
        self.backend.order_status.return_value = {"found": False, "uncertain": True, "orderStatus": "unknownOid"}
        second = reconcile_cancel(self.db, self.backend, "cancel-original", ids[1])
        self.assertEqual(second["state"], "unknown")
        self.db._conn.close()
        self.db = Database(self.path)
        saved = cancellations(self.db, self.backend)["items"][0]
        self.assertEqual(set(saved["evidence"]["targets"]), set(ids))
        self.assertEqual(saved["result"], original)
        self.assertEqual(self.db.claim_write_request("cancel-original", "/api/cancel", payload)["result"], original)
        calls = self.backend.order_status.call_count
        with self.assertRaises(HyperliquidError):
            reconcile_cancel(self.db, self.backend, "cancel-original", "999")
        self.backend.network = "testnet"
        with self.assertRaises(HyperliquidError):
            reconcile_cancel(self.db, self.backend, "cancel-original", ids[0])
        self.assertEqual(self.backend.order_status.call_count, calls)

    def test_account_cancel_readback_uses_each_targets_symbol(self):
        payload = {**self.payload, "body": {"targets": [{"symbol": "HL_APT", "orderId": "123"},
                                                       {"symbol": "HL_BTC", "orderId": "456"}]}}
        self.db.claim_write_request("cancel-account", "/api/cancel", payload)
        saved = cancellations(self.db, self.backend)["items"][0]
        self.assertEqual(saved["symbols"], {"123": "HL_APT", "456": "HL_BTC"})
        self.backend.order_status.return_value = {"found": True, "symbol": "HL_BTC", "order_id": "456", "orderStatus": "canceled"}
        result = reconcile_cancel(self.db, self.backend, "cancel-account", "456")
        self.assertEqual(result["state"], "observed")
        self.backend.order_status.return_value["symbol"] = "HL_APT"
        with self.assertRaises(HyperliquidError):
            reconcile_cancel(self.db, self.backend, "cancel-account", "456")
        self.db._conn.close()
        self.db = Database(self.path)
        self.assertEqual(cancellations(self.db, self.backend)["items"][0]["evidence"]["targets"]["456"]["status"]["symbol"], "HL_BTC")

    def test_parallel_cancel_readbacks_preserve_both_targets(self):
        from concurrent.futures import ThreadPoolExecutor
        payload = {**self.payload, "body": {"symbol": "HL_APT", "orderIds": ["123", "456"]}}
        self.db.claim_write_request("cancel-parallel", "/api/cancel", payload)
        self.backend.order_status.side_effect = lambda target: {"found": True, "order_id": target,
                                                               "symbol": "HL_APT", "orderStatus": "canceled"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda target: reconcile_cancel(self.db, self.backend, "cancel-parallel", target), ["123", "456"]))
        item = cancellations(self.db, self.backend)["items"][0]
        self.assertEqual(set(item["evidence"]["targets"]), {"123", "456"})
        self.assertEqual(self.db.claim_write_request("cancel-parallel", "/api/cancel", payload)["state"], "pending")

    def test_cancel_client_identity_mismatch_and_simulation_are_not_reconciled(self):
        payload = {**self.payload, "body": {"symbol": "HL_APT", "cliOrdId": self.cloid}}
        self.db.claim_write_request("cancel-client", "/api/cancel", payload)
        self.backend.order_status.return_value = {"found": True, "symbol": "HL_BTC", "cliOrdId": self.cloid}
        with self.assertRaises(HyperliquidError):
            reconcile_cancel(self.db, self.backend, "cancel-client", self.cloid)
        self.db.complete_write_request("cancel-client", 200, {"outcome": "simulated"})
        self.backend.order_status.reset_mock()
        with self.assertRaises(HyperliquidError):
            reconcile_cancel(self.db, self.backend, "cancel-client", self.cloid)
        self.backend.order_status.assert_not_called()

    def test_cancel_history_is_bounded_but_exact_request_lookup_can_find_older_rows(self):
        payload = {**self.payload, "body": {"symbol": "HL_APT", "orderId": "123"}}
        for n in range(22):
            self.db.claim_write_request(f"cancel-{n}", "/api/cancel", payload)
        result = cancellations(self.db, self.backend)
        self.assertTrue(result["hasMore"])
        self.assertEqual(len(result["items"]), 20)
        self.assertEqual(cancellations(self.db, self.backend, "cancel-0")["items"][0]["requestId"], "cancel-0")
        for invalid in ("bad", "", {}, "a" * 101, "not/valid"):
            with self.subTest(invalid=invalid), self.assertRaises(HyperliquidError):
                cancellations(self.db, self.backend, invalid)
        self.backend.account_address = "0x" + "2" * 40
        self.assertEqual(cancellations(self.db, self.backend)["items"], [])

    def test_simultaneous_new_clients_cannot_both_claim_an_order(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(self.claim, ["concurrent-first", "concurrent-second"]))
        self.assertEqual(sorted(item["state"] for item in outcomes), ["blocked", "new"])

    def test_prepared_action_is_immutable_durable_and_account_scoped(self):
        self.claim()
        action = {"type": "order", "orders": [{"s": "19.99", "c": self.cloid}], "grouping": "na"}
        self.db.prepare_hyperliquid_order("request-first", action)
        self.db.prepare_hyperliquid_order("request-first", action)
        with self.assertRaises(ValueError):
            self.db.prepare_hyperliquid_order("request-first", {**action, "grouping": "positionTpsl"})
        self.db._conn.close()
        self.db = Database(self.path)
        self.assertEqual(self.db.venue_prepared_order("request-first", "mainnet", self.account), action)
        self.assertIsNone(self.db.venue_prepared_order("request-first", "testnet", self.account))
        self.assertIsNone(self.db.venue_prepared_order("request-first", "mainnet", "0x" + "2" * 40))
        self.db.complete_write_request("request-first", 200, {"outcome": "confirmed"})
        with self.assertRaises(ValueError):
            self.db.prepare_hyperliquid_order("request-first", action)

    def test_pending_submission_survives_restart_and_blocks_another_client(self):
        self.assertEqual(self.claim()["state"], "new")
        self.db._conn.close()
        self.db = Database(self.path)
        items = unresolved(self.db, self.backend)["items"]
        self.assertEqual(items[0]["requestId"], "request-first")
        self.assertEqual(items[0]["cloid"], self.cloid)
        self.assertEqual(self.claim("request-second")["state"], "blocked")
        self.assertEqual(self.claim()["state"], "pending", "same identity must never be resent")

    def test_reconciliation_keeps_original_unknown_receipt_and_unblocks_new_intent(self):
        self.claim()
        original = {"outcome": "unknown", "error": "response lost"}
        self.db.complete_write_request("request-first", 200, original)
        result = reconcile(self.db, self.backend, "request-first")
        self.assertEqual(result["status"]["order_id"], "12345678901234567890")
        self.backend.order_status.assert_called_once_with(self.cloid)
        self.assertEqual(unresolved(self.db, self.backend)["items"], [])
        self.assertEqual(self.claim()["result"], original, "readback must not rewrite execution history")
        self.assertEqual(self.claim("request-second")["state"], "new")

    def test_unknown_or_wrong_identity_does_not_release_block(self):
        self.claim()
        for status in ({"found": False, "uncertain": True},
                       {"found": True, "cliOrdId": self.cloid, "symbol": "HL_BTC"},
                       {"found": True, "cliOrdId": "0x" + "b" * 32, "symbol": "HL_APT"}):
            self.backend.order_status.return_value = status
            with self.assertRaises(HyperliquidError):
                reconcile(self.db, self.backend, "request-first")
            self.assertEqual(self.claim("request-second")["state"], "blocked")

    def test_network_account_and_kraken_are_isolated(self):
        self.claim()
        for changes in ({"network": "testnet"}, {"account": "0x" + "2" * 40}):
            scoped = {**self.payload, **changes}
            self.assertEqual(self.claim("request-" + next(iter(changes)), scoped)["state"], "new")
        self.assertEqual(self.db.claim_write_request("kraken-request", "/api/order", {"symbol": "PF_APTUSD"})["state"], "new")
        self.assertEqual(len(unresolved(self.db, self.backend)["items"]), 1)
        self.backend.account_address = "0x" + "3" * 40
        with self.assertRaises(HyperliquidError):
            reconcile(self.db, self.backend, "request-first")
        self.backend.order_status.assert_not_called()

    def test_completed_known_outcome_does_not_block(self):
        for index, outcome in enumerate(("confirmed", "rejected", "simulated")):
            request = f"request-{index}"
            self.assertEqual(self.claim(request)["state"], "new")
            self.db.complete_write_request(request, 200, {"outcome": outcome})
        self.assertEqual(unresolved(self.db, self.backend)["items"], [])

    def test_partial_or_corrupt_result_remains_unresolved(self):
        self.claim()
        self.db.complete_write_request("request-first", 200, {"outcome": "partial"})
        self.assertEqual(self.claim("request-next")["state"], "blocked")
        self.db._conn.execute("UPDATE write_requests SET result_json='not json'")
        self.db._conn.commit()
        self.assertEqual(self.claim("request-next")["state"], "blocked")


if __name__ == "__main__":
    unittest.main()
