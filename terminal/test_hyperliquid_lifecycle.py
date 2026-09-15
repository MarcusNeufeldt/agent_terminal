import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from db import Database
from hyperliquid_client import HyperliquidError
from hyperliquid_fills import normalize
from hyperliquid_lifecycle import CANCELED, inspect_order


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "lifecycle.db")
        self.account = "0x" + "1" * 40
        self.cloid = "0x" + "a" * 32
        self.oid = 12345678901234567890
        self.body = {"symbol": "HL_APT", "side": "buy", "size": 1, "reduceOnly": False, "cloid": self.cloid}
        self.db.claim_write_request("request-original", "/api/order", {"venue": "hyperliquid", "network": "mainnet",
                                   "account": self.account, "body": self.body})
        self.status = {"found": True, "orderStatus": "open", "order_id": str(self.oid), "cliOrdId": self.cloid,
                       "symbol": "HL_APT", "side": "buy", "reduceOnly": False,
                       "originalSizeExact": "1", "remainingSizeExact": "0.6"}
        self.backend = SimpleNamespace(network="mainnet", account_address=self.account, account_configured=True,
                                       order_status=Mock(side_effect=lambda _: dict(self.status)))

    def tearDown(self):
        self.db._conn.close()
        self.temp.cleanup()

    def fill(self, size, tid=1, **changes):
        raw = {"coin": "APT", "side": "B", "oid": self.oid, "tid": tid, "time": 1000,
               "sz": size, "px": "0.6", "fee": "0", "feeToken": "USDC", "closedPnl": "0", **changes}
        self.db.save_hyperliquid_fill_page("mainnet", self.account, [normalize(raw)], 1000, 1000, "available-window-scanned")

    def inspect(self):
        return inspect_order(self.db, self.backend, "request-original")

    def test_open_partial_fills_match_without_authorizing_replacement(self):
        self.fill("0.1", 1)
        self.fill("0.3", 2)
        result = self.inspect()
        self.assertEqual(result["lifecycle"], "open")
        self.assertEqual(result["fillEvidence"], "matched")
        self.assertEqual(result["reportedExecutedSize"], "0.4")
        self.assertEqual(result["fills"]["observedFilledSize"], "0.4")
        self.assertEqual(result["automationDecision"], "monitor")
        self.assertFalse(result["canReplace"])
        self.assertFalse(result["historyComplete"])

    def test_filled_status_waits_for_missing_fills(self):
        self.status.update(orderStatus="filled", remainingSizeExact="0")
        self.fill("0.4")
        result = self.inspect()
        self.assertTrue(result["terminal"])
        self.assertEqual(result["fillEvidence"], "missing")
        self.assertEqual(result["automationDecision"], "wait")
        self.fill("0.6", 2)
        result = self.inspect()
        self.assertEqual(result["fillEvidence"], "matched")
        self.assertEqual(result["automationDecision"], "stop")
        self.assertFalse(result["canReplace"])

    def test_exchange_cancellation_is_terminal_even_with_partial_fill_history(self):
        self.fill("0.1")
        for kind in CANCELED:
            with self.subTest(kind=kind):
                self.status["orderStatus"] = kind
                result = self.inspect()
                self.assertTrue(result["terminal"])
                self.assertEqual(result["automationDecision"], "stop")
                self.assertEqual(result["fillEvidence"], "observed-only")
                self.assertFalse(result["canReplace"])

    def test_snapshot_races_and_overfill_remain_unresolved(self):
        self.fill("0.5")
        self.assertEqual(self.inspect()["fillEvidence"], "conflicting")
        self.status["remainingSizeExact"] = "0.5"
        self.assertEqual(self.inspect()["fillEvidence"], "matched")
        self.fill("0.6", 2)
        result = self.inspect()
        self.assertEqual(result["fillEvidence"], "conflicting")
        self.assertEqual(result["automationDecision"], "wait")

    def test_lot_rounding_uses_the_persisted_validated_size(self):
        self.db.complete_write_request("request-original", 200, {"outcome": "simulated"})
        self.db.claim_write_request("request-normalized", "/api/order", {"venue": "hyperliquid", "network": "mainnet",
                                   "account": self.account, "body": {**self.body, "size": 1.009}})
        self.db.prepare_hyperliquid_order("request-normalized", {"type": "order", "orders": [
            {"s": "1", "c": self.cloid, "b": True, "r": False}]})
        self.fill("0.4")
        result = inspect_order(self.db, self.backend, "request-normalized")
        self.assertEqual(result["requestedSize"], "1.009")
        self.assertEqual(result["validatedSize"], "1")
        self.assertTrue(result["hasPreparedOrder"])
        self.assertEqual(result["fillEvidence"], "matched")
        self.assertFalse(result["canReplace"])

    def test_changed_exact_size_or_intent_blocks_automation(self):
        self.status["originalSizeExact"] = "1.0000000000000000001"
        result = self.inspect()
        self.assertEqual(result["automationDecision"], "stop")
        self.assertEqual(result["fillEvidence"], "conflicting")
        self.status["originalSizeExact"] = "1"
        for key, value in (("side", "sell"), ("reduceOnly", True), ("symbol", "HL_BTC"), ("cliOrdId", "0x" + "b" * 32)):
            old = self.status[key]
            self.status[key] = value
            with self.subTest(key=key), self.assertRaises(HyperliquidError):
                self.inspect()
            self.status[key] = old

    def test_unknown_and_triggered_are_not_fills(self):
        for kind in ("futureStatus", "triggered"):
            self.status["orderStatus"] = kind
            self.assertEqual(self.inspect()["automationDecision"], "wait")
        self.status = {"found": False, "uncertain": True, "orderStatus": "unknownOid"}
        self.assertIsNone(self.inspect()["terminal"])
        self.assertFalse(self.inspect()["canReplace"])

    def test_rejected_order_with_fills_is_a_conflict(self):
        self.status["orderStatus"] = "rejected"
        self.assertEqual(self.inspect()["automationDecision"], "stop")
        self.fill("0.1")
        self.assertEqual(self.inspect()["fillEvidence"], "conflicting")

    def test_foreign_account_request_never_queries_exchange(self):
        self.backend.account_address = "0x" + "2" * 40
        with self.assertRaises(HyperliquidError):
            self.inspect()
        self.backend.order_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
