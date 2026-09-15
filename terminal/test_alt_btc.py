import ast
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import alt_btc

NOW = 1_800_000_000_000


def quote(symbol, opened, last, **extra):
    return {"symbol": symbol, "openPrice": str(opened), "lastPrice": str(last), "quoteVolume": "1000000",
            "openTime": NOW - 86_400_000, "closeTime": NOW, **extra}


def instrument(symbol, **extra):
    return {"symbol": symbol, "tradeable": True, **extra}


def candles(end_ms, step=1, count=72):
    start = end_ms - count * alt_btc.CANDLE_MS
    return [[start + i * alt_btc.CANDLE_MS, str(100 + step * i), "999", "1",
             str(100 + step * (i + 1)), "10", start + (i + 1) * alt_btc.CANDLE_MS - 1, "1000"]
            for i in range(count)]


class AltBtcTests(unittest.TestCase):
    def test_ratio_return_is_not_percentage_point_subtraction(self):
        data = alt_btc.build_snapshot([instrument("PF_SOLUSD")],
                                      [quote("BTCUSDT", 100, 105), quote("SOLUSDT", 10, 11)], NOW)
        self.assertAlmostEqual(data["rows"][0]["changeVsBtcPct"], (1.1 / 1.05 - 1) * 100)
        self.assertAlmostEqual(data["rows"][0]["changeUsdtPct"], 10)
        self.assertAlmostEqual(data["rows"][0]["priceBtc"], 11 / 105)
        self.assertAlmostEqual(data["btcChangePct"], 5)

    def test_falling_alt_can_outperform_btc_and_rows_sort_best_first(self):
        data = alt_btc.build_snapshot([instrument("PF_SOLUSD"), instrument("PF_ETHUSD")], [
            quote("BTCUSDT", 100, 90), quote("SOLUSDT", 100, 97), quote("ETHUSDT", 100, 85)], NOW)
        self.assertEqual([r["asset"] for r in data["rows"]], ["SOL", "ETH"])
        self.assertGreater(data["rows"][0]["changeVsBtcPct"], 0)
        self.assertLess(data["rows"][0]["changeUsdtPct"], 0)
        self.assertLess(data["rows"][1]["changeVsBtcPct"], 0)

    def test_only_tradable_kraken_altcoin_contracts_are_included(self):
        contracts = [instrument("PF_XBTUSD"), instrument("PF_SOLUSD"), instrument("PF_SOLUSD"),
                     instrument("PF_OFFUSD", tradeable=False), instrument("PF_EXPIREDUSD", isExpired=True),
                     instrument("PF_STOCKUSD", tradfi=True), instrument("PI_ETHUSD"), instrument("PF_MISSINGUSD")]
        tickers = [quote(s + "USDT", 100, 100) for s in ["BTC", "SOL", "OFF", "EXPIRED", "STOCK", "ETH", "ONLYBINANCE"]]
        data = alt_btc.build_snapshot(contracts, tickers, NOW)
        self.assertEqual([r["symbol"] for r in data["rows"]], ["PF_SOLUSD"])
        self.assertEqual(data["eligibleCount"], 2)
        self.assertEqual(data["excludedCount"], 1)

    def test_stale_incomplete_or_invalid_quotes_are_omitted(self):
        for extra in [{"closeTime": NOW - 300_000}, {"openTime": NOW - 3_600_000},
                      {"openPrice": "0"}, {"lastPrice": "NaN"}, {"quoteVolume": "Infinity"}]:
            with self.subTest(extra=extra):
                data = alt_btc.build_snapshot([instrument("PF_SOLUSD")],
                    [quote("BTCUSDT", 100, 100), quote("SOLUSDT", 100, 100, **extra)], NOW)
                self.assertEqual(data["rows"], [])
                self.assertEqual(data["excludedCount"], 1)

    def test_missing_benchmark_or_instruments_fail_not_zero_returns(self):
        for contracts, tickers in [([], []), (None, []), ([instrument("PF_SOLUSD")], []),
                                  ([instrument("PF_SOLUSD")], [quote("BTCUSDT", 100, 100, closeTime=0)])]:
            with self.subTest(contracts=contracts), self.assertRaises(ValueError):
                alt_btc.build_snapshot(contracts, tickers, NOW)

    def test_bulk_fetch_is_cached_without_private_credentials(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps([quote("BTCUSDT", 100, 100), quote("SOLUSDT", 10, 11)]).encode()
        with patch.object(alt_btc, "_cache", None), patch.object(alt_btc.time, "time", return_value=NOW / 1000), \
                patch.object(alt_btc, "urlopen", return_value=response) as opening:
            first = alt_btc.get_snapshot([instrument("PF_SOLUSD")])
            second = alt_btc.get_snapshot([instrument("PF_SOLUSD")])
            self.assertEqual(first, second)
            self.assertEqual(opening.call_count, 1)
            request = opening.call_args.args[0]
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.full_url, alt_btc.TICKERS_URL)
            self.assertNotIn("apikey", {k.lower() for k in request.headers})

    def test_rate_limits_stop_requests_until_retry_after(self):
        for status, retry_after, delay in [(429, "120", 120), (429, "NaN", 60), (418, "5", 600)]:
            with self.subTest(status=status, retry_after=retry_after), \
                    patch.object(alt_btc, "_blocked_until", 0), \
                    patch.object(alt_btc.time, "monotonic", return_value=100) as clock, \
                    patch.object(alt_btc, "urlopen") as opening:
                opening.side_effect = HTTPError(alt_btc.TICKERS_URL, status, "rate limit", {"Retry-After": retry_after}, None)
                for _ in range(2):
                    with self.assertRaises(HTTPError):
                        alt_btc._read_json(alt_btc.TICKERS_URL)
                self.assertEqual(opening.call_count, 1)
                self.assertEqual(alt_btc._blocked_until, 100 + delay)
                clock.return_value = 101 + delay
                with self.assertRaises(HTTPError):
                    alt_btc._read_json(alt_btc.TICKERS_URL)
                self.assertEqual(opening.call_count, 2, "requests resume only after cooldown")

    def test_get_endpoint_reports_source_failure_as_unavailable(self):
        tree = ast.parse(Path(__file__).with_name("server.py").read_text())
        handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TerminalHandler")
        method = next(node for node in handler.body if isinstance(node, ast.FunctionDef) and node.name == "do_GET")
        from threading import RLock
        from exchange_routing import ExchangeRouting, ExchangeRoutingError, requested_exchange
        namespace = {"urlparse": urlparse, "parse_qs": parse_qs, "alt_btc": alt_btc,
                     "exchange_routing": ExchangeRouting(RLock()), "ExchangeRoutingError": ExchangeRoutingError,
                     "requested_exchange": requested_exchange,
                     "get_instruments": lambda: {"instruments": [instrument("PF_SOLUSD")]}}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "isolated_altbtc_get", "exec"), namespace)
        receiver = SimpleNamespace(path="/api/alt-btc", _send_json=Mock())
        with patch.object(alt_btc, "get_snapshot", side_effect=ValueError("Binance offline")):
            namespace["do_GET"](receiver)
        body, status = receiver._send_json.call_args.args
        self.assertEqual(status, 503)
        self.assertEqual(body["state"], "unavailable")
        self.assertIn("Binance offline", body["error"])
        for window in ("1h", "6h", "24h"):
            receiver.path = f"/api/alt-btc?window={window}"
            with patch.object(alt_btc, "get_snapshot", return_value={"window": window}) as fetch:
                namespace["do_GET"](receiver)
                self.assertEqual(fetch.call_args.kwargs["window"], window)
                self.assertEqual(receiver._send_json.call_args.args[0]["window"], window)
        receiver.path = "/api/alt-btc?window=2h"
        with patch.object(alt_btc, "get_snapshot") as fetch:
            namespace["do_GET"](receiver)
            fetch.assert_not_called()
            self.assertEqual(receiver._send_json.call_args.args[1], 400)


class ShortWindowTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.stamp = None
        self.overrides = {}
        self.contracts = [instrument("PF_SOLUSD"), instrument("PF_XBTUSD"),
                          instrument("PF_MISSINGUSD"), instrument("PF_OFFUSD", tradeable=False)]
        self.enterContext(patch.object(alt_btc, "_cache", None))
        self.enterContext(patch.object(alt_btc, "_short_cache", None))
        self.enterContext(patch.object(alt_btc.time, "time", side_effect=lambda: self.now / 1000))
        self.enterContext(patch.object(alt_btc.time, "monotonic", side_effect=lambda: self.now / 1000))
        self.fetch = self.enterContext(patch.object(alt_btc, "_read_json", side_effect=self.read))

    def read(self, url):
        if url == alt_btc.TICKERS_URL:
            return [quote(s + "USDT", 100, 105, closeTime=self.now if self.stamp is None else self.stamp,
                          openTime=self.now - alt_btc.WINDOWS["24h"]) for s in ("BTC", "SOL", "OFF", "ONLYBINANCE")]
        params = parse_qs(urlparse(url).query)
        self.assertEqual(params["interval"], ["5m"])
        self.assertEqual(params["limit"], ["72"])
        end = int(params["endTime"][0]) + 1
        self.assertEqual(int(params["startTime"][0]), end - alt_btc.WINDOWS["6h"])
        symbol = params["symbol"][0]
        self.assertIn(symbol, ("BTCUSDT", "SOLUSDT"), "never fetch unavailable or non-Kraken symbols")
        value = self.overrides.get(symbol, candles(end, step=1 if symbol == "BTCUSDT" else 2))
        if isinstance(value, Exception):
            raise value
        return value

    def test_distinct_short_returns_and_volume_share_one_candle_fetch(self):
        hour = alt_btc.get_snapshot(self.contracts, "1h")
        six = alt_btc.get_snapshot(self.contracts, "6h")
        day = alt_btc.get_snapshot(self.contracts)
        self.assertEqual(self.fetch.call_count, 3, "one catalog plus BTC and SOL history, not per window")
        for data, period, alt_open, btc_open, volume in [
                (hour, "1h", 220, 160, 12_000), (six, "6h", 100, 100, 72_000)]:
            self.assertEqual(data["window"], period)
            self.assertEqual(data["granularity"], "5m")
            self.assertEqual(data["asOfEpochMs"], NOW - 1)
            self.assertEqual(data["windowStartEpochMs"], NOW - alt_btc.WINDOWS[period])
            self.assertEqual(data["eligibleCount"], 2)
            self.assertEqual(data["excludedCount"], 1)
            self.assertEqual([row["asset"] for row in data["rows"]], ["SOL"])
            row = data["rows"][0]
            self.assertAlmostEqual(row["changeVsBtcPct"], ((244 / alt_open) / (172 / btc_open) - 1) * 100)
            self.assertAlmostEqual(row["priceBtc"], 244 / 172)
            self.assertEqual(row["volumeUsdt"], volume)
        self.assertEqual(day["window"], "24h")
        self.assertAlmostEqual(day["rows"][0]["changeVsBtcPct"], 0)
        with self.assertRaisesRegex(ValueError, "Window must"):
            alt_btc.get_snapshot(self.contracts, "2h")
        self.assertEqual(self.fetch.call_count, 3)

    def test_incomplete_six_hour_history_does_not_hide_valid_hour(self):
        self.overrides["SOLUSDT"] = candles(NOW, count=12)
        self.assertEqual(len(alt_btc.get_snapshot(self.contracts, "1h")["rows"]), 1)
        self.assertEqual(alt_btc.get_snapshot(self.contracts, "6h")["rows"], [])
        self.assertEqual(self.fetch.call_count, 3)

    def test_candle_gaps_duplicates_bad_volume_and_misalignment_fail_closed(self):
        good = candles(NOW)
        for bad in [good[:-1], good + [good[-1]], good[:65] + good[66:], list(reversed(good))]:
            with self.subTest(bad=bad[:1]), self.assertRaises(ValueError):
                alt_btc.candle_quote("SOLUSDT", bad, "1h", NOW)
        for index, value in [(6, NOW), (7, "NaN"), (7, "-1")]:
            bad = [list(row) for row in good]
            bad[-1][index] = value
            with self.subTest(index=index, value=value), self.assertRaises(ValueError):
                alt_btc.candle_quote("SOLUSDT", bad, "1h", NOW)
        btc = alt_btc.candle_quote("BTCUSDT", good, "1h", NOW)
        shifted = alt_btc.candle_quote("SOLUSDT", candles(NOW - alt_btc.CANDLE_MS), "1h", NOW - alt_btc.CANDLE_MS)
        result = alt_btc.build_snapshot(self.contracts, [btc, shifted], NOW, "1h")
        self.assertEqual(result["rows"], [])
        for field, value in [("openPrice", "0"), ("lastPrice", "Infinity")]:
            alt = {**btc, "symbol": "SOLUSDT", field: value}
            self.assertEqual(alt_btc.build_snapshot(self.contracts, [btc, alt], NOW, "1h")["rows"], [])

    def test_unavailable_coin_is_omitted_and_missing_benchmark_stops_fetching(self):
        self.overrides["SOLUSDT"] = OSError("public feed offline")
        self.assertEqual(alt_btc.get_snapshot(self.contracts, "1h")["rows"], [])
        self.now += alt_btc.CANDLE_MS
        self.overrides["BTCUSDT"] = []
        calls = self.fetch.call_count
        with self.assertRaisesRegex(ValueError, "benchmark unavailable"):
            alt_btc.get_snapshot(self.contracts, "1h")
        self.assertEqual(self.fetch.call_count, calls + 2, "stop before fetching coins without a benchmark")
        self.assertEqual(alt_btc.get_snapshot(self.contracts, "24h")["state"], "current")

    def test_rate_limit_cancels_remaining_fanout_and_never_caches_partial_result(self):
        self.overrides["SOLUSDT"] = HTTPError(alt_btc.BINANCE_BASE, 429, "rate limit", {}, None)
        with self.assertRaises(HTTPError):
            alt_btc.get_snapshot(self.contracts, "1h")
        self.assertIsNone(alt_btc._short_cache)
        stop = threading.Event()
        with self.assertRaises(HTTPError):
            alt_btc._short_quotes("SOLUSDT", NOW, stop)
        self.assertTrue(stop.is_set())
        calls = self.fetch.call_count
        self.assertEqual(alt_btc._short_quotes("OTHERUSDT", NOW, stop), {})
        self.assertEqual(self.fetch.call_count, calls)

    def test_exchange_clock_anchor_rollover_and_stale_clock(self):
        self.now = NOW + 1_000
        self.stamp = NOW - 1  # Local clock has crossed the boundary; Binance has not.
        first = alt_btc.get_snapshot(self.contracts, "1h")
        self.assertEqual(first["asOfEpochMs"], NOW - alt_btc.CANDLE_MS - 1)
        alt_btc.get_snapshot(self.contracts, "6h")
        self.assertEqual(self.fetch.call_count, 3)
        self.now += alt_btc.CANDLE_MS
        self.stamp = None
        second = alt_btc.get_snapshot(self.contracts, "1h")
        self.assertGreater(second["asOfEpochMs"], first["asOfEpochMs"])
        self.assertEqual(self.fetch.call_count, 6)
        self.now += alt_btc.CANDLE_MS
        self.stamp = self.now - 400_000
        with self.assertRaisesRegex(ValueError, "clock reference unavailable"):
            alt_btc.get_snapshot(self.contracts, "1h")
        self.assertEqual(self.fetch.call_count, 7, "stale source must not launch a candle fanout")


if __name__ == "__main__":
    unittest.main()
