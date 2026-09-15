import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from db import Database
from hyperliquid_client import HyperliquidError
from hyperliquid_fills import normalize, order_totals, sync


class FillHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "fills.db"
        self.db = Database(self.path)
        self.account = "0x" + "1" * 40
        self.oid = 12345678901234567890
        self.data = [self.fill(index, index) for index in range(5)]
        self.backend = SimpleNamespace(network="mainnet", account_address=self.account,
                                       account_configured=True, client=SimpleNamespace(info=Mock(side_effect=self.info)))
        self.cap = patch("hyperliquid_fills.PAGE_LIMIT", 3)
        self.cap.start()

    def tearDown(self):
        self.cap.stop()
        self.db._conn.close()
        self.temp.cleanup()

    def fill(self, tid, timestamp, **changes):
        return {"coin": "APT", "side": "B", "oid": self.oid, "tid": tid, "time": timestamp,
                "sz": "0.1", "px": "0.6", "fee": "0.0001", "feeToken": "USDC", "closedPnl": "0", **changes}

    def info(self, kind, **params):
        self.assertEqual(kind, "userFillsByTime")
        self.assertFalse(params["aggregateByTime"])
        self.assertEqual(params["user"], self.account)
        return [fill for fill in self.data if params["startTime"] <= fill["time"] <= params["endTime"]][:3]

    def test_bisection_deduplicates_inclusive_pages_and_keeps_exact_totals(self):
        result = sync(self.db, self.backend, 0, 9)
        self.assertTrue(result["scanComplete"])
        self.assertFalse(result["historyComplete"])
        self.assertEqual(result["inserted"], 5)
        totals = order_totals(self.db, self.backend, str(self.oid))
        self.assertEqual(totals["observedFilledSize"], "0.5")
        self.assertEqual(totals["averagePrice"], "0.6")
        self.assertEqual(totals["observedFees"], {"USDC": "0.0005"})
        self.assertEqual(totals["fillCount"], 5)
        self.assertFalse(totals["historyComplete"])
        self.assertNotIn("remainingSize", totals)
        self.db._conn.close()
        self.db = Database(self.path)
        self.assertEqual(sync(self.db, self.backend, 0, 9)["inserted"], 0)
        self.assertEqual(order_totals(self.db, self.backend, str(self.oid))["fillCount"], 5)

    def test_full_single_timestamp_is_a_gap_not_false_completeness(self):
        self.data = [self.fill(index, 1) for index in range(4)]
        result = sync(self.db, self.backend, 1, 1)
        self.assertFalse(result["scanComplete"])
        self.assertEqual(result["gaps"][0]["reason"], "timestamp-saturated")
        self.assertEqual(order_totals(self.db, self.backend, str(self.oid))["fillCount"], 3)

    def test_concentrated_burst_is_located_without_bisecting_an_empty_month(self):
        self.data = [self.fill(index, 123) for index in range(4)]
        result = sync(self.db, self.backend, 0, 30 * 86400000)
        self.assertLessEqual(result["pages"], 5)
        self.assertEqual(result["gaps"], [{"startTime": 123, "endTime": 123, "reason": "timestamp-saturated"}])

    def test_missing_fees_and_realized_pnl_stay_unknown(self):
        self.data = [self.fill(1, 1, fee=None, closedPnl=None)]
        sync(self.db, self.backend, 0, 9)
        totals = order_totals(self.db, self.backend, str(self.oid))
        self.assertFalse(totals["feesKnown"])
        self.assertEqual(totals["observedFees"], {})
        self.assertIsNone(totals["observedRealizedPnl"])

    def test_budget_exhaustion_keeps_checkpointed_rows(self):
        with patch("hyperliquid_fills.MAX_PAGES", 1):
            result = sync(self.db, self.backend, 0, 9)
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(result["inserted"], 3)
        self.assertEqual(len(result["gaps"]), 2)
        self.assertEqual(order_totals(self.db, self.backend, str(self.oid))["fillCount"], 3)

    def test_mid_scan_failure_preserves_earlier_page(self):
        first = self.info("userFillsByTime", user=self.account, startTime=0, endTime=9, aggregateByTime=False)
        self.backend.client.info.side_effect = [first, HyperliquidError("rate limited")]
        result = sync(self.db, self.backend, 0, 9)
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(result["inserted"], 3)
        self.assertIn("rate limited", result["gaps"][0]["reason"])

    def test_conflicting_duplicate_rolls_back_only_new_page(self):
        self.data = [self.fill(1, 1)]
        sync(self.db, self.backend, 0, 9)
        self.data = [self.fill(2, 2), self.fill(1, 1, sz="0.2")]
        result = sync(self.db, self.backend, 0, 9)
        self.assertEqual(result["state"], "incomplete")
        totals = order_totals(self.db, self.backend, str(self.oid))
        self.assertEqual(totals["fillCount"], 1)
        self.assertEqual(totals["observedFilledSize"], "0.1")

    def test_other_network_or_account_has_no_observed_fills(self):
        sync(self.db, self.backend, 0, 9)
        self.backend.network = "testnet"
        totals = order_totals(self.db, self.backend, str(self.oid))
        self.assertEqual(totals["fillCount"], 0)
        self.assertIsNone(totals["averagePrice"])
        self.assertFalse(totals["historyComplete"])
        self.backend.network = "mainnet"
        self.backend.account_address = "0x" + "2" * 40
        self.assertEqual(order_totals(self.db, self.backend, str(self.oid))["fillCount"], 0)

    def test_invalid_data_or_window_never_becomes_zero_accounting(self):
        for changes in ({"oid": True}, {"tid": -1}, {"sz": "NaN"}, {"px": "0"},
                        {"fee": "Infinity"}, {"side": "?"}, {"time": 1.5}):
            with self.subTest(changes=changes), self.assertRaises(HyperliquidError):
                normalize({**self.fill(1, 1), **changes})
        with self.assertRaises(HyperliquidError):
            sync(self.db, self.backend, 10, 1)
        self.backend.client.info.assert_not_called()
        self.backend.client.info.return_value = [self.fill(1, 99)]
        self.backend.client.info.side_effect = None
        result = sync(self.db, self.backend, 0, 9)
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(result["inserted"], 0)


if __name__ == "__main__":
    unittest.main()
