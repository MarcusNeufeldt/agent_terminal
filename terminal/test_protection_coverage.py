import unittest

import actions


class ProtectionCoverageTests(unittest.TestCase):
    """Whole-position protection cover, including Hyperliquid's zero-size sentinel."""

    def test_sized_reduce_only_orders_cover_once_they_reach_position_size(self):
        orders = [{"symbol": "HL_UNI", "orderType": "stp", "reduceOnly": True, "unfilledSize": 4}]
        self.assertFalse(actions.protection_covers(orders, "HL_UNI", "SL", 6))
        orders.append({"symbol": "HL_UNI", "orderType": "stop", "reduceOnly": True, "unfilledSize": 2})
        self.assertTrue(actions.protection_covers(orders, "HL_UNI", "SL", 6))

    def test_native_position_tpsl_covers_despite_the_zero_size_sentinel(self):
        # Hyperliquid reports position-level protection with size 0. The exchange
        # resolves the whole position at trigger time, so it always covers, and
        # summing the reported size would leave the alert permanently unclearable.
        orders = [{"symbol": "HL_UNI", "orderType": "stp", "reduceOnly": True,
                   "unfilledSize": 0, "size": 0, "positionTpsl": True}]
        self.assertTrue(actions.protection_covers(orders, "HL_UNI", "SL", 6))
        take_profit = [{"symbol": "HL_UNI", "orderType": "take_profit", "reduceOnly": True,
                        "unfilledSize": 0, "size": 0, "positionTpsl": True}]
        self.assertTrue(actions.protection_covers(take_profit, "HL_UNI", "TP", 6))

    def test_zero_size_without_the_position_flag_does_not_cover(self):
        orders = [{"symbol": "HL_UNI", "orderType": "stp", "reduceOnly": True, "unfilledSize": 0}]
        self.assertFalse(actions.protection_covers(orders, "HL_UNI", "SL", 6))

    def test_position_tpsl_of_the_other_kind_does_not_cover(self):
        orders = [{"symbol": "HL_UNI", "orderType": "take_profit", "reduceOnly": True,
                   "unfilledSize": 0, "positionTpsl": True}]
        self.assertFalse(actions.protection_covers(orders, "HL_UNI", "SL", 6))

    def test_other_symbols_and_non_reduce_only_orders_are_ignored(self):
        orders = [
            {"symbol": "HL_ETH", "orderType": "stp", "reduceOnly": True, "unfilledSize": 0, "positionTpsl": True},
            {"symbol": "HL_UNI", "orderType": "stp", "reduceOnly": False, "unfilledSize": 9},
        ]
        self.assertFalse(actions.protection_covers(orders, "HL_UNI", "SL", 6))

    def test_kraken_string_booleans_and_size_fallback_still_work(self):
        # Kraken reports reduceOnly as a string and may omit unfilledSize.
        orders = [{"symbol": "PF_UNIUSD", "orderType": "stop", "reduceOnly": "true", "size": 6}]
        self.assertTrue(actions.protection_covers(orders, "PF_UNIUSD", "SL", 6))

    def test_unreadable_sizes_do_not_count_as_cover(self):
        orders = [{"symbol": "HL_UNI", "orderType": "stp", "reduceOnly": True, "unfilledSize": "not a number"}]
        self.assertFalse(actions.protection_covers(orders, "HL_UNI", "SL", 6))

    def test_a_flat_or_unknown_position_size_is_never_reported_as_covered(self):
        orders = [{"symbol": "HL_UNI", "orderType": "stp", "reduceOnly": True, "unfilledSize": 0, "positionTpsl": True}]
        self.assertFalse(actions.protection_covers(orders, "HL_UNI", "SL", 0))
        self.assertFalse(actions.protection_covers(orders, "HL_UNI", "SL", None))


if __name__ == "__main__":
    unittest.main()
