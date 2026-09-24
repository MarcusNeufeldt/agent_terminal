import time
import unittest

import hyperliquid_chart
from hyperliquid_client import HyperliquidError

CLOID = "0xf5c256b647e9802dab11d6f34fd2320a"
MISREAD = {"outcome": "rejected", "rows": [{"state": "error", "error": "waitingForTrigger"}],
           "error": "waitingForTrigger"}


class FakeDb:
    def __init__(self, result):
        self.result, self.saved = result, None

    def venue_recovery_state(self, request_id, network, account):
        return {"result": self.result}

    def save_write_reconciliation(self, request_id, evidence, target=None):
        self.saved = evidence


class FakeBackend:
    network, account_address = "mainnet", "0x" + "1" * 40

    def orderbook(self, symbol, fresh=False):
        return {"time": time.time() * 1000}

    def order_status(self, ident):
        return {"found": True, "orderStatus": "open", "cliOrdId": CLOID, "symbol": "HL_HYPE", "reduceOnly": True,
                "side": "sell", "positionTpsl": True, "isTrigger": True, "triggerPrice": "94.994", "order_id": "555739575992"}


def intent(expired_ms_ago=60000):
    return {"type": "chartIntent", "symbol": "HL_HYPE", "kind": "tp", "fullPosition": True, "target": None,
            "expiresAfter": int(time.time() * 1000) - expired_ms_ago,
            "action": {"type": "order", "grouping": "positionTpsl", "orders": [{
                "a": 159, "b": False, "p": "94.994", "s": "0", "r": True, "c": CLOID,
                "t": {"trigger": {"isMarket": True, "triggerPx": "94.994", "tpsl": "tp"}}}]}}


class ChartRecoveryTests(unittest.TestCase):
    def test_a_trigger_misread_as_rejected_can_still_be_reconciled(self):
        db = FakeDb(MISREAD)
        evidence = hyperliquid_chart.reconcile(db, FakeBackend(), "req-1", {"symbol": "HL_HYPE", "cloid": CLOID}, intent())
        self.assertEqual((evidence["outcome"], evidence["kind"], evidence["canReplace"]), ("reconciled", "chart", False))
        self.assertEqual(db.saved, evidence)

    def test_a_real_rejection_still_has_nothing_to_reconcile(self):
        real = {"outcome": "rejected", "rows": [{"state": "error", "error": "Insufficient margin"}]}
        for result in (real, {"outcome": "simulated"}, {"outcome": "rejected", "rows": []}):
            with self.assertRaisesRegex(HyperliquidError, "No uncertain live chart operation"):
                hyperliquid_chart.reconcile(FakeDb(result), FakeBackend(), "req-2",
                                            {"symbol": "HL_HYPE", "cloid": CLOID}, intent())

    def test_the_misread_request_still_waits_for_its_signed_expiry(self):
        with self.assertRaisesRegex(HyperliquidError, "expire"):
            hyperliquid_chart.reconcile(FakeDb(MISREAD), FakeBackend(), "req-3",
                                        {"symbol": "HL_HYPE", "cloid": CLOID}, intent(expired_ms_ago=-60000))


if __name__ == "__main__":
    unittest.main()
