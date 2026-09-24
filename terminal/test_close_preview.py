import unittest

from close_preview import TAKER_FEE, close_preview, sorted_levels

BCH_BOOK = {"bids": [[342.25, 10], [342.0, 30], [341.71, 40]], "asks": [[342.5, 50]]}
BCH_LONG = {"side": "long", "size": 51.8, "price": 341.18}


class ClosePreviewTwinTests(unittest.TestCase):
    """Same cases as frontend/src/close-preview.test.js, so the AI and the screen agree."""

    def test_kraken_worst_first_bids_are_walked_best_first(self):
        levels = sorted_levels([[150, 0.76], [341.71, 20], [342.25, 10], [342.0, 30]], "bids")
        self.assertEqual([px for px, _ in levels], [342.25, 342.0, 341.71, 150])

    def test_the_bch_close_matches_the_screen(self):
        p = close_preview(BCH_LONG, BCH_BOOK, fee_rate=TAKER_FEE["kraken"])
        avg = (10 * 342.25 + 30 * 342.0 + 11.8 * 341.71) / 51.8
        self.assertAlmostEqual(p["avgExitPrice"], avg)
        self.assertAlmostEqual(p["grossAtBest"], 51.8 * (342.25 - 341.18))
        self.assertAlmostEqual(p["bookWalkCost"], 51.8 * (342.25 - avg))
        self.assertAlmostEqual(p["exitFee"], 0.0005 * 51.8 * avg)
        self.assertAlmostEqual(p["netIfClosed"], p["grossAtBest"] - p["bookWalkCost"] - p["exitFee"])
        self.assertEqual(p["levelsUsed"], 3)

    def test_a_short_closes_into_the_asks_and_funding_is_added(self):
        p = close_preview({"side": "short", "size": 3, "price": 110},
                          {"bids": [[99, 5]], "asks": [[101, 1], [100, 1], [102, 5]]}, fee_rate=0, funding=2.5)
        self.assertEqual(p["bestPrice"], 100)
        self.assertAlmostEqual(p["netIfClosed"], 3 * (110 - 101) + 2.5)

    def test_size_beyond_the_visible_book_is_valued_at_the_worst_level(self):
        p = close_preview({"side": "long", "size": 10, "price": 100}, {"bids": [[102, 4], [101, 2]]}, fee_rate=0.001)
        walked = 4 * 2 + 2 * 1 - 0.001 * (4 * 102 + 2 * 101)
        self.assertAlmostEqual(p["netIfClosed"], walked + 4 * (101 - 100) - 0.001 * 4 * 101)
        self.assertEqual(p["beyondVisibleBook"], 4)

    def test_the_entry_fee_makes_net_the_whole_trade_and_matches_the_js_twin(self):
        exit_only = close_preview(BCH_LONG, BCH_BOOK, fee_rate=TAKER_FEE["kraken"])
        trade = close_preview(BCH_LONG, BCH_BOOK, fee_rate=TAKER_FEE["kraken"], entry_fee_rate=TAKER_FEE["kraken"])
        self.assertEqual(exit_only["entryFee"], 0.0)
        self.assertAlmostEqual(trade["entryFee"], 0.0005 * 51.8 * 341.18)
        self.assertAlmostEqual(trade["netIfClosed"], exit_only["netIfClosed"] - 0.0005 * 51.8 * 341.18)

    def test_contract_multiplier_and_unusable_input(self):
        p = close_preview({"side": "long", "size": 2, "price": 100}, {"bids": [[110, 5]]}, fee_rate=0.001,
                          contract_size=10, funding=-1.5)
        self.assertAlmostEqual(p["netIfClosed"], 200 - 2.2 - 1.5)
        self.assertIsNone(close_preview({"side": "flat", "size": 1, "price": 1}, {"bids": [[1, 1]]}, fee_rate=0))
        self.assertIsNone(close_preview(BCH_LONG, {"bids": []}, fee_rate=0))
        self.assertIsNone(close_preview({"side": "long", "size": 0, "price": 1}, {"bids": [[1, 1]]}, fee_rate=0))


class AiNetPnlTests(unittest.TestCase):
    """with_net_if_closed, AST-loaded from server.py without starting the server."""

    def setUp(self):
        import ast
        from pathlib import Path
        from typing import Any
        import close_preview as close_preview_module
        source = ast.parse(Path(__file__).with_name("server.py").read_text(encoding="utf-8"))
        wanted = {"with_net_if_closed", "_as_float"}
        nodes = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
        self.assertEqual({n.name for n in nodes}, wanted)
        self.book_calls = []
        test = self

        class Client:
            def get(self, path, params=None):
                test.book_calls.append(params["symbol"])
                if params["symbol"] == "PF_DOWNUSD":
                    raise OSError("book down")
                # Kraken order: bids worst-first.
                return {"orderBook": {"bids": [[150, 1], [341.71, 40], [342.0, 30], [342.25, 10]], "asks": [[342.5, 50]]}}

        class Hub:
            def ticker(self, symbol):
                return {"bid": 100.0, "ask": 101.0}

        self.ns = {"Any": Any, "close_preview": close_preview_module, "client": Client(), "hub": Hub(), "AI_NET_BOOKS": 8,
                   "get_instruments": lambda: {"instruments": [
                       {"symbol": "PF_BCHUSD", "type": "flexible_futures", "contractSize": 1},
                       {"symbol": "PF_DOWNUSD", "type": "flexible_futures", "contractSize": 1},
                       {"symbol": "PI_XBTUSD", "type": "futures_inverse", "contractSize": 1}]}}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "isolated_net", "exec"), self.ns)

    def test_the_ai_gets_the_screen_net_and_kraken_mark_pnl_only_under_its_own_name(self):
        rows = self.ns["with_net_if_closed"]([
            {"symbol": "PF_BCHUSD", "side": "long", "size": 51.8, "price": 341.18, "unrealizedPnl": 74.1, "unrealizedFunding": 0},
            {"symbol": "PF_DOWNUSD", "side": "long", "size": 2, "price": 90, "unrealizedPnl": 20, "unrealizedFunding": 1},
            {"symbol": "PI_XBTUSD", "side": "long", "size": 100, "price": 60000, "unrealizedPnl": 0.001},
            {"error": "positions unavailable"},
        ])
        bch, down, inverse, error = rows
        expected = close_preview(BCH_LONG, BCH_BOOK, fee_rate=TAKER_FEE["kraken"],
                                 entry_fee_rate=TAKER_FEE["kraken"])["netIfClosed"]
        self.assertAlmostEqual(bch["netIfClosed"], round(expected, 4))
        self.assertEqual(bch["netBasis"], "book")
        self.assertNotIn("unrealizedPnl", bch, "the mark-based figure never goes out under the PnL name")
        self.assertEqual(bch["krakenMarkPnl"], 74.1)
        self.assertEqual(down["netBasis"], "best_price_no_depth", "a failed book read falls back to best bid less the fee")
        self.assertAlmostEqual(down["netIfClosed"], 2 * (100 - 90) - 0.0005 * 2 * 100 - 0.0005 * 2 * 90 + 1)
        self.assertEqual(inverse["netBasis"], "unavailable")
        self.assertNotIn("netIfClosed", inverse)
        self.assertEqual(error, {"error": "positions unavailable"})
        self.assertNotIn("PI_XBTUSD", self.book_calls, "no book read for a position it cannot value")

    def test_the_snapshot_quotes_net_and_the_total(self):
        import ai_chat
        snapshot = ai_chat.build_context_snapshot(
            account={"balanceValue": 7000, "totalUnrealized": 74.1}, symbol="PF_BCHUSD", ticker=None, candles=[], orders=[],
            positions=[{"symbol": "PF_BCHUSD", "side": "long", "size": 51.8, "price": 341.18, "netIfClosed": 32.6967,
                        "grossAtBest": 55.426, "krakenMarkPnl": 74.1, "netBasis": "book"},
                       {"symbol": "PF_ETHUSD", "side": "short", "size": 1, "price": 3000, "netIfClosed": -10.0}])
        self.assertIn('"netIfClosed": 32.6967', snapshot)
        self.assertIn("NET IF CLOSED, ALL POSITIONS: 22.7", snapshot)
        self.assertNotIn("totalUnrealized", snapshot, "Kraken's mark-based account total is not offered")
        self.assertNotIn('"unrealizedPnl"', snapshot)


if __name__ == "__main__":
    unittest.main()
