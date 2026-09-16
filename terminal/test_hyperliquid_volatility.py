import time
import unittest

import scanner
from hyperliquid_client import HyperliquidError


def candles(coin, closes, *, minutes_ago_start=20):
    """1m rows shaped like Hyperliquid's candleSnapshot, all safely in the past."""
    minute = int(time.time() // 60) * 60_000
    rows = []
    for index, close in enumerate(closes):
        start = minute - (minutes_ago_start - index) * 60_000
        rows.append({"t": start, "T": start + 59_999, "s": coin, "i": "1m",
                     "o": str(close), "c": str(close), "h": str(close * 1.001),
                     "l": str(close * 0.999), "v": "10", "n": 5})
    return rows


class FakeClient:
    def __init__(self, universe, contexts, candle_map):
        self.universe, self.contexts, self.candle_map = universe, contexts, candle_map
        self.fetched = []

    def info(self, kind, **params):
        if kind == "metaAndAssetCtxs":
            return [{"universe": self.universe}, self.contexts]
        if kind == "candleSnapshot":
            coin = params["req"]["coin"]
            self.fetched.append(coin)
            if coin not in self.candle_map:
                raise HyperliquidError("no candles")
            return self.candle_map[coin]
        raise AssertionError("unexpected info request " + kind)


class FakeBackend:
    def __init__(self, client, coins):
        self.client = client
        self._coins = coins
        self.markets_calls = 0

    def markets(self):
        self.markets_calls += 1
        return {"HL_" + coin: {"instrument": {"coin": coin, "tradeable": True}} for coin in self._coins}


def context(mark, volume, spread=0.0001, impact=True):
    ctx = {"markPx": str(mark), "dayNtlVlm": str(volume)}
    if impact:
        ctx["impactPxs"] = [str(mark * (1 - spread / 2)), str(mark * (1 + spread / 2))]
    return ctx


class HyperliquidVolatilityScanTests(unittest.TestCase):
    def setUp(self):
        scanner._hl_vol_cache.clear()

    def test_ranks_by_realized_volatility_and_reports_the_venue(self):
        universe = [{"name": "WILD"}, {"name": "CALM"}]
        contexts = [context(100, 5_000_000), context(50, 9_000_000)]
        client = FakeClient(universe, contexts, {
            "WILD": candles("WILD", [100, 104, 99, 106, 101, 108, 102]),
            "CALM": candles("CALM", [50, 50.01, 50, 50.01, 50, 50.01, 50]),
        })
        result = scanner.scan_volatility_hyperliquid(FakeBackend(client, ["WILD", "CALM"]))
        self.assertEqual(result["exchange"], "hyperliquid")
        self.assertEqual(result["spreadBasis"], "impact")
        self.assertEqual(result["cached"], False)
        self.assertEqual([row["symbol"] for row in result["rows"]], ["HL_WILD", "HL_CALM"])
        self.assertGreater(result["rows"][0]["realizedVolatilityPercent"],
                           result["rows"][1]["realizedVolatilityPercent"])

    def test_thin_and_wide_markets_are_dropped_before_any_candle_is_fetched(self):
        universe = [{"name": "GOOD"}, {"name": "THIN"}, {"name": "WIDE"}, {"name": "DEAD"}]
        contexts = [
            context(100, 5_000_000),
            context(100, 1_000),                      # under the volume floor
            context(100, 5_000_000, spread=0.05),     # 5% wide, over the spread cap
            context(100, 5_000_000, impact=False),    # delisted: quotes nothing
        ]
        client = FakeClient(universe, contexts, {"GOOD": candles("GOOD", [100, 101, 100, 101, 100, 101, 100])})
        result = scanner.scan_volatility_hyperliquid(FakeBackend(client, ["GOOD", "THIN", "WIDE", "DEAD"]))
        self.assertEqual(client.fetched, ["GOOD"], "only survivors cost a request")
        self.assertEqual(result["marketsScanned"], 1)
        self.assertEqual([row["symbol"] for row in result["rows"]], ["HL_GOOD"])

    def test_a_market_whose_candles_fail_does_not_sink_the_scan(self):
        universe = [{"name": "GOOD"}, {"name": "BROKEN"}]
        contexts = [context(100, 5_000_000), context(100, 6_000_000)]
        client = FakeClient(universe, contexts, {"GOOD": candles("GOOD", [100, 101, 100, 101, 100, 101, 100])})
        result = scanner.scan_volatility_hyperliquid(FakeBackend(client, ["GOOD", "BROKEN"]))
        self.assertEqual(sorted(client.fetched), ["BROKEN", "GOOD"])
        self.assertEqual([row["symbol"] for row in result["rows"]], ["HL_GOOD"])

    def test_an_incomplete_history_is_omitted_rather_than_guessed(self):
        universe = [{"name": "SHORT"}]
        contexts = [context(100, 5_000_000)]
        client = FakeClient(universe, contexts, {"SHORT": candles("SHORT", [100, 101])})
        result = scanner.scan_volatility_hyperliquid(FakeBackend(client, ["SHORT"]))
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["marketsScanned"], 1)

    def test_repeat_scans_are_served_from_cache(self):
        universe = [{"name": "GOOD"}]
        contexts = [context(100, 5_000_000)]
        client = FakeClient(universe, contexts, {"GOOD": candles("GOOD", [100, 101, 100, 101, 100, 101, 100])})
        backend = FakeBackend(client, ["GOOD"])
        first = scanner.scan_volatility_hyperliquid(backend)
        second = scanner.scan_volatility_hyperliquid(backend)
        self.assertEqual(first["cached"], False)
        self.assertEqual(second["cached"], True)
        self.assertEqual(client.fetched, ["GOOD"], "a cache hit costs no candle request")
        # A different window is a different scan.
        scanner.scan_volatility_hyperliquid(backend, window_minutes=3)
        self.assertEqual(client.fetched, ["GOOD", "GOOD"])

    def test_a_malformed_universe_is_rejected_rather_than_half_scanned(self):
        client = FakeClient([{"name": "GOOD"}], [], {})
        with self.assertRaises(HyperliquidError):
            scanner.scan_volatility_hyperliquid(FakeBackend(client, ["GOOD"]))
        self.assertEqual(client.fetched, [])

    def test_kraken_and_hyperliquid_caches_stay_separate(self):
        universe = [{"name": "GOOD"}]
        contexts = [context(100, 5_000_000)]
        client = FakeClient(universe, contexts, {"GOOD": candles("GOOD", [100, 101, 100, 101, 100, 101, 100])})
        scanner.scan_volatility_hyperliquid(FakeBackend(client, ["GOOD"]))
        self.assertEqual(scanner._vol_cache, {}, "the Kraken scan must not serve Hyperliquid rows")


if __name__ == "__main__":
    unittest.main()
