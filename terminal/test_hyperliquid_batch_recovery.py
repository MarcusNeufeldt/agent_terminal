import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from db import Database
from hyperliquid_batch_recovery import advance, prepared_orders, receipt_observations
from hyperliquid_client import HyperliquidError
from hyperliquid_recovery import unresolved


class BatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.db = Database(self.path)
        self.account = "0x" + "1" * 40
        self.ids = ["0x" + "a" * 32, "0x" + "b" * 32]
        self.payload = {"venue": "hyperliquid", "network": "mainnet", "account": self.account,
                        "body": {"symbol": "HL_APT", "orders": 2}}
        self.action = {"type": "order", "grouping": "na", "orders": [
            {"a": 1, "b": True, "r": False, "s": str(2 + i), "p": "0.6", "c": cloid,
             "t": {"limit": {"tif": "Alo"}}} for i, cloid in enumerate(self.ids)]}
        self.db.claim_write_request("batch-original", "/api/grid", self.payload)
        self.db.prepare_hyperliquid_order("batch-original", self.action)
        self.backend = SimpleNamespace(network="mainnet", account_address=self.account, account_configured=True,
                                       order_status=Mock(side_effect=self.status))

    def status(self, cloid):
        index = self.ids.index(cloid)
        return {"found": True, "uncertain": False, "cliOrdId": cloid, "order_id": str(101 + index),
                "symbol": "HL_APT", "side": "buy", "reduceOnly": False, "orderStatus": "open",
                "originalSizeExact": str(2 + index), "remainingSizeExact": str(2 + index)}

    def tearDown(self):
        self.db._conn.close()
        self.temp.cleanup()

    def claim_next(self):
        return self.db.claim_write_request("next-order", "/api/order", self.payload)

    def test_one_read_per_call_and_restart_preserves_partial_barrier(self):
        original = {"outcome": "unknown", "error": "response lost"}
        self.db.complete_write_request("batch-original", 200, original)
        item = unresolved(self.db, self.backend)["items"][0]
        self.assertEqual(item["cloids"], self.ids)
        first = advance(self.db, self.backend, "batch-original")
        self.assertEqual((first["remaining"], first["outcome"]), (1, "unknown"))
        self.assertFalse(first["canReplace"])
        self.assertEqual(self.claim_next()["state"], "blocked")
        self.db._conn.close()
        self.db = Database(self.path)
        final = advance(self.db, self.backend, "batch-original")
        self.assertEqual((final["remaining"], final["outcome"]), (0, "reconciled"))
        self.assertEqual([call.args[0] for call in self.backend.order_status.call_args_list], self.ids)
        self.assertEqual(self.claim_next()["state"], "new")
        self.assertEqual(self.db.venue_recovery_state("batch-original", "mainnet", self.account)["result"], original)

    def test_unknown_target_does_not_prevent_checking_the_next_identity(self):
        self.backend.order_status.side_effect = lambda _: {"found": False, "uncertain": True, "orderStatus": "unknownOid"}
        self.assertEqual(advance(self.db, self.backend, "batch-original")["remaining"], 2)
        self.backend.order_status.side_effect = self.status
        self.assertEqual(advance(self.db, self.backend, "batch-original")["remaining"], 1)
        self.assertEqual(self.backend.order_status.call_args.args[0], self.ids[1])
        self.assertEqual(self.claim_next()["state"], "blocked")
        self.assertEqual(advance(self.db, self.backend, "batch-original")["remaining"], 0)

    def test_complete_partial_receipt_proves_each_outcome_without_upstream_reads(self):
        original = {"type": "order", "action": self.action, "outcome": "partial",
                    "rows": [{"state": "resting", "oid": 101}, {"state": "error", "error": "margin"}]}
        self.db.complete_write_request("batch-original", 200, original)
        result = advance(self.db, self.backend, "batch-original")
        self.assertEqual(result["outcome"], "reconciled")
        self.assertEqual(result["targets"][self.ids[1]]["state"], "rejected")
        self.assertFalse(result["canReplace"])
        self.backend.order_status.assert_not_called()
        self.assertEqual(self.claim_next()["state"], "new")

    def test_missing_receipt_rows_cannot_prove_other_targets(self):
        self.db.complete_write_request("batch-original", 200, {"type": "order", "action": self.action,
            "outcome": "partial", "rows": [{"state": "resting", "oid": 101}]})
        result = advance(self.db, self.backend, "batch-original")
        self.assertEqual(result["remaining"], 1)
        self.backend.order_status.assert_called_once_with(self.ids[0])
        self.assertEqual(self.claim_next()["state"], "blocked")

    def test_duplicate_order_ids_and_mismatched_sizes_leave_the_batch_blocked(self):
        advance(self.db, self.backend, "batch-original")
        self.backend.order_status.side_effect = lambda cloid: {**self.status(cloid), "order_id": "101"}
        result = advance(self.db, self.backend, "batch-original")
        self.assertIn("Duplicate", result["targets"][self.ids[1]]["error"])
        self.backend.order_status.side_effect = lambda cloid: {**self.status(cloid), "originalSizeExact": "999"}
        self.assertEqual(advance(self.db, self.backend, "batch-original")["remaining"], 1)
        self.assertEqual(self.claim_next()["state"], "blocked")

    def test_concurrent_readbacks_merge_and_late_failure_cannot_erase_verified_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda cloid: advance(self.db, self.backend, "batch-original", cloid), self.ids))
        result = self.db.save_batch_observation("batch-original", self.ids[0], {"state": "unknown", "error": "late timeout"})
        self.assertEqual(result["outcome"], "reconciled")
        self.assertEqual(result["targets"][self.ids[0]]["state"], "observed")

    def test_partial_or_corrupt_evidence_does_not_unlock_and_scope_is_checked(self):
        self.db.save_write_reconciliation("batch-original", {"outcome": "unknown", "targets": {}})
        self.assertEqual(self.claim_next()["state"], "blocked")
        self.db._conn.execute("UPDATE write_reconciliations SET evidence_json='bad json'")
        self.db._conn.commit()
        self.assertEqual(self.claim_next()["state"], "blocked")
        self.backend.network = "testnet"
        with self.assertRaises(HyperliquidError):
            advance(self.db, self.backend, "batch-original")
        self.backend.order_status.assert_not_called()

    def test_malformed_receipt_rows_cannot_be_used_as_submission_proof(self):
        good = {"state": "resting", "oid": 101}
        for bad in ({"state": "error", "error": "margin", "oid": 102}, {"state": []},
                    {"state": "resting", "oid": 101},
                    {"state": "filled", "oid": 102, "totalSize": "999", "averagePrice": "0.6"}):
            original = {"type": "order", "action": self.action, "outcome": "partial", "rows": [good, bad]}
            self.assertIsNone(receipt_observations(original, self.action, self.action["orders"]))

    def test_batch_identities_must_be_unique_and_bounded(self):
        for orders in ([self.action["orders"][0]] * 2, self.action["orders"] * 11):
            with self.assertRaises(HyperliquidError):
                prepared_orders({**self.action, "orders": orders})


if __name__ == "__main__":
    unittest.main()
