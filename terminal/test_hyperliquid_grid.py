"""Planner tests use metadata fixtures only. No account or exchange transport."""
import unittest
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from grid import GridError, build_grid_plan
from hyperliquid_client import HyperliquidError
from hyperliquid_grid import prepare, preview
from hyperliquid_trading import format_price, format_size


class HyperliquidGridTests(unittest.TestCase):
    def setUp(self):
        self.instrument = {"symbol": "HL_APT", "tradeable": True, "contractValueTradePrecision": 2, "assetId": 1}
        self.backend = SimpleNamespace(network="testnet", markets=lambda: {"HL_APT": {"instrument": self.instrument}})
        self.spec = {"symbol": "HL_APT", "side": "buy", "startPrice": "0.60006", "endPrice": "0.58004",
                     "orders": 3, "notional": 100, "orderType": "post"}

    def test_shared_sizing_uses_native_side_conservative_prices(self):
        for side in ("buy", "sell"):
            spec = dict(self.spec, side=side)
            if side == "sell":
                spec["startPrice"], spec["endPrice"] = spec["endPrice"], spec["startPrice"]
            result = preview(spec, self.backend)
            self.assertFalse(result["ready"])
            self.assertTrue(result["previewOnly"])
            plan = result["plan"]
            self.assertEqual(len(plan["orders"]), 3)
            self.assertLessEqual(Decimal(str(plan["notional"])), Decimal(100))
            start, end = Decimal(spec["startPrice"]), Decimal(spec["endPrice"])
            for i, row in enumerate(plan["orders"]):
                raw = start + (end - start) * i / 2
                price = Decimal(str(row["limitPrice"]))
                self.assertTrue(price <= raw if side == "buy" else price >= raw)
                self.assertEqual(price, Decimal(format_price(row["limitPrice"], 2)))
                self.assertEqual(Decimal(str(row["size"])), Decimal(format_size(row["size"], 2)))
            self.assertEqual(plan["previewHash"], preview(spec, self.backend)["plan"]["previewHash"])

    def test_contract_budget_minimum_warning_and_invalid_scope(self):
        spec = {key: value for key, value in self.spec.items() if key != "notional"}
        result = preview({**spec, "size": "1.009"}, self.backend)
        self.assertEqual(result["plan"]["totalSize"], 1)
        self.assertTrue(any("below $10" in warning for warning in result["plan"]["warnings"]))
        for patch in ({"symbol": "PF_APTUSD"}, {"orders": 21}, {"endPrice": "0.60005"}, {"notional": "1e1000000"}):
            with self.subTest(patch=patch), self.assertRaises((GridError, HyperliquidError)):
                preview({**self.spec, **patch}, self.backend)
        with self.assertRaises(GridError):
            build_grid_plan(self.spec, self.instrument)  # Kraken defaults must not accept HL.

    def prepared_spec(self):
        return {**self.spec, "previewHash": preview(self.spec, self.backend)["plan"]["previewHash"]}

    def test_prepared_batch_preserves_reviewed_rungs_and_distinct_client_ids(self):
        self.install_reads()
        spec = self.prepared_spec()
        ids = ["0x" + f"{i:032x}" for i in range(1, 4)]
        action = prepare(spec, self.backend, ids)
        self.assertEqual(action["type"], "order")
        self.assertEqual(action["grouping"], "na")
        self.assertEqual([row["c"] for row in action["orders"]], ids)
        plan = preview(spec, self.backend)["plan"]
        for wire, row in zip(action["orders"], plan["orders"]):
            self.assertEqual(Decimal(wire["p"]), Decimal(str(row["limitPrice"])))
            self.assertEqual(Decimal(wire["s"]), Decimal(str(row["size"])))
            self.assertEqual(wire["t"], {"limit": {"tif": "Alo"}})
        ids[0] = "changed"
        self.assertNotEqual(action["orders"][0]["c"], ids[0])
        self.backend.orders.assert_not_called()
        self.backend.positions.assert_not_called()
        self.backend.orderbook.assert_not_called()

    def test_preparation_uses_one_metadata_snapshot_and_supports_bounded_reduce_only_limits(self):
        spec = {key: value for key, value in self.spec.items() if key != "notional"}
        spec.update(side="sell", startPrice="0.58004", endPrice="0.60006", size="60.009", orderType="lmt", reduceOnly=True)
        spec["previewHash"] = preview(spec, self.backend)["plan"]["previewHash"]
        self.backend.markets = Mock(return_value={"HL_APT": {"instrument": self.instrument}})
        action = prepare(spec, self.backend, ["0x" + f"{i:032x}" for i in range(1, 4)])
        self.backend.markets.assert_called_once()
        self.assertEqual(sum(Decimal(row["s"]) for row in action["orders"]), Decimal("60"))
        for row in action["orders"]:
            self.assertFalse(row["b"])
            self.assertTrue(row["r"])
            self.assertEqual(row["t"], {"limit": {"tif": "Gtc"}})

    def test_preparation_rejects_stale_preview_bad_ids_and_unsupported_intent(self):
        spec = self.prepared_spec()
        ids = ["0x" + f"{i:032x}" for i in range(1, 4)]
        for bad in (None, [], ids[:2], [ids[0]] * 3, ["bad"] * 3, ids * 7):
            with self.subTest(ids=bad), self.assertRaises(GridError):
                prepare(spec, self.backend, bad)
        for patch in ({"previewHash": "stale"}, {"notional": 101}, {"triggerMarket": True},
                      {"cloid": ids[0]}, {"cloids": list(reversed(ids))}):
            with self.subTest(patch=patch), self.assertRaises(GridError):
                prepare({**spec, **patch}, self.backend, ids)
        small = {**self.spec, "notional": 10}
        small["previewHash"] = preview(small, self.backend)["plan"]["previewHash"]
        with self.assertRaisesRegex(GridError, "minimum"):
            prepare(small, self.backend, ids)
        self.instrument["assetId"] = True
        with self.assertRaisesRegex(GridError, "identity"):
            prepare(spec, self.backend, ids)

    def test_prepared_partial_batch_survives_adapter_and_journal_restart_without_retry(self):
        import tempfile
        from pathlib import Path
        from db import Database
        from hyperliquid_trading import HyperliquidTrader
        from hyperliquid_batch_recovery import advance
        spec = self.prepared_spec()
        ids = ["0x" + f"{i:032x}" for i in range(1, 4)]
        action = prepare(spec, self.backend, ids)
        transport = SimpleNamespace(post=Mock(return_value={"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"resting": {"oid": 101}}, {"error": "insufficient margin"},
            {"filled": {"oid": 102, "totalSz": action["orders"][2]["s"], "avgPx": action["orders"][2]["p"]}}
        ]}}}))
        account = "0x" + "1" * 40
        trader = HyperliquidTrader(lambda _: self.instrument, transport=transport,
            private_key="1".zfill(64), account_address=account, network="testnet")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.db"
            db = Database(path)
            try:
                db.claim_write_request("grid-prepared-test", "/api/grid", {"venue": "hyperliquid", "network": "testnet",
                    "account": account, "body": {**spec, "cloids": ids}})
                db.prepare_hyperliquid_order("grid-prepared-test", action)
                # The HTTP write wrapper adds the action type to trader results.
                result = {"type": "order", **trader.submit(action, count=3)}
                self.assertEqual(result["outcome"], "partial")
                db.complete_write_request("grid-prepared-test", 200, result)
                db._conn.close()
                db = Database(path)
                backend = SimpleNamespace(network="testnet", account_address=account, account_configured=True,
                                          order_status=Mock(side_effect=AssertionError("Known rows need no readback")))
                recovered = advance(db, backend, "grid-prepared-test")
                self.assertEqual(recovered["remaining"], 0)
                self.assertFalse(recovered["canReplace"])
                self.assertEqual(recovered["targets"][ids[1]]["state"], "rejected")
                self.assertEqual(db.venue_recovery_state("grid-prepared-test", "testnet", account)["result"], result)
                backend.order_status.assert_not_called()
                transport.post.assert_called_once()
                next_ids = ["0x" + f"{i:032x}" for i in range(4, 7)]
                next_action = prepare(spec, self.backend, next_ids)
                payload = {"venue": "hyperliquid", "network": "testnet", "account": account,
                           "body": {**spec, "cloids": next_ids}}
                self.assertEqual(db.claim_write_request("grid-timeout-test", "/api/grid", payload)["state"], "new")
                db.prepare_hyperliquid_order("grid-timeout-test", next_action)
                transport.post.side_effect = HyperliquidError("Response lost")
                lost = {"type": "order", **trader.submit(next_action, count=3)}
                self.assertEqual(lost["outcome"], "unknown")
                db.complete_write_request("grid-timeout-test", 200, lost)
                db._conn.close()
                db = Database(path)
                backend.order_status.side_effect = None
                backend.order_status.return_value = {"found": False, "uncertain": True, "orderStatus": "unknownOid"}
                self.assertEqual(advance(db, backend, "grid-timeout-test")["remaining"], 3)
                self.assertEqual(db.claim_write_request("grid-blocked-test", "/api/grid", payload)["state"], "blocked")
                self.assertEqual(transport.post.call_count, 2, "One submission for each distinct intent, no recovery retry")
            finally:
                db._conn.close()

    def install_reads(self):
        self.backend.orderbook = Mock(return_value={"time": time.time() * 1000, "orderBook": {"bids": [[0.5, 10]], "asks": [[0.7, 10]]}})
        self.backend.positions = Mock(return_value={"positions": []})
        self.backend.orders = Mock(return_value={"orders": []})

    def test_current_checks_are_opt_in_fresh_and_never_enable_placement(self):
        self.install_reads()
        self.assertIsNone(preview(self.spec, self.backend)["orderChecksPassed"])
        self.backend.orderbook.assert_not_called()
        result = preview({**self.spec, "checkCurrentOrders": True}, self.backend)
        self.assertTrue(result["orderChecksPassed"])
        self.assertFalse(result["ready"])
        self.assertTrue(result["previewOnly"])
        self.assertTrue(result["orderCheckedAt"])
        self.backend.orderbook.assert_called_once_with("HL_APT", fresh=True)
        self.backend.positions.assert_called_once_with(fresh=True)
        self.backend.orders.assert_called_once_with(fresh=True)
        with self.assertRaises(GridError):
            preview({**self.spec, "checkCurrentOrders": "true"}, self.backend)

    def test_duplicate_crossing_and_read_failure_preserve_plan_but_fail_checks(self):
        self.install_reads()
        spec = {**self.spec, "checkCurrentOrders": True}
        price = preview(self.spec, self.backend)["plan"]["orders"][0]["limitPrice"]
        self.backend.orders.return_value = {"orders": [{"symbol": "HL_APT", "side": "buy", "limitPrice": price}]}
        result = preview(spec, self.backend)
        self.assertFalse(result["orderChecksPassed"])
        self.assertIn("already exists", result["validationError"])
        self.backend.orders.return_value = {"orders": []}
        self.backend.orderbook.return_value["orderBook"]["asks"] = [[0.59, 10]]
        self.assertIn("cross", preview(spec, self.backend)["validationError"])
        self.backend.orders.side_effect = HyperliquidError("Order read failed")
        result = preview(spec, self.backend)
        self.assertFalse(result["orderChecksPassed"])
        self.assertEqual(result["validationError"], "Order read failed")
        self.assertEqual(len(result["plan"]["orders"]), 3)

    def test_current_checks_reject_stale_missing_and_future_quotes(self):
        self.install_reads()
        for stamp in (None, True, float("nan"), time.time() * 1000 - 60000, time.time() * 1000 + 60000):
            self.backend.orderbook.return_value["time"] = stamp
            result = preview({**self.spec, "checkCurrentOrders": True}, self.backend)
            self.assertFalse(result["orderChecksPassed"])
            self.assertIn("timestamp", result["validationError"])
        self.backend.positions.assert_not_called()

    def test_reduce_only_uses_exact_exposure_and_existing_exit_reservations(self):
        self.install_reads()
        spec = {k: v for k, v in self.spec.items() if k != "notional"}
        spec.update(side="sell", startPrice="0.58", endPrice="0.6", size=3,
                    reduceOnly=True, checkCurrentOrders=True)
        self.backend.positions.return_value = {"positions": [{"symbol": "HL_APT", "side": "long", "size": 4, "sizeExact": "3"}]}
        self.backend.orders.return_value = {"orders": [{"symbol": "HL_APT", "side": "sell", "orderType": "lmt",
            "reduceOnly": True, "limitPrice": 0.8, "unfilledSize": 0, "unfilledSizeExact": "0.01"}]}
        self.assertIn("exceeds", preview(spec, self.backend)["validationError"])
        self.backend.orders.return_value = {"orders": []}
        self.assertTrue(preview(spec, self.backend)["orderChecksPassed"])
        self.backend.positions.return_value["positions"][0]["side"] = "short"
        self.assertIn("opposite side", preview(spec, self.backend)["validationError"])

    def test_formatters_fail_cleanly_for_unrepresentable_values(self):
        for formatter in (format_price, format_size):
            for precision in (True, -1, 7, "2"):
                with self.subTest(formatter=formatter.__name__, precision=precision), self.assertRaises(HyperliquidError):
                    formatter(1, precision)
            with self.assertRaises(HyperliquidError):
                formatter("1e100", 2)


if __name__ == "__main__":
    unittest.main()
