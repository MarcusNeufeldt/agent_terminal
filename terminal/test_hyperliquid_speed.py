"""Latency work on the Hyperliquid order path: parallel pre-trade reads, a cached
signer check, and the streamed order book. Every safety check must still run."""

import time
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from hyperliquid_backend import HyperliquidBackend
import test_hyperliquid_trading as base
from test_hyperliquid_trading import ACCOUNT, MARKETS, SIGNER, make_trader


def done(value=None, error=None):
    future = Future()
    future.set_exception(error) if error else future.set_result(value)
    return future


class SignerCacheTests(unittest.TestCase):
    # Borrow the isolated server-write harness without re-running its own tests.
    setUp = base.WriteDecisionTests.setUp
    write = base.WriteDecisionTests.write
    order = base.WriteDecisionTests.order

    def test_the_signer_check_runs_once_then_is_trusted_for_five_minutes(self):
        self.ns["armed"] = True
        agent = self.ns["hyperliquid"].require_agent
        self.write("/api/order", self.order())
        self.write("/api/order", self.order())
        self.assertEqual(agent.call_count, 1, "the second order skips the ~280 ms signer lookup")
        self.assertEqual(len(self.trader.calls), 2)
        self.ns["_agent_checked"]["at"] -= 301
        self.write("/api/order", self.order())
        self.assertEqual(agent.call_count, 2, "after five minutes it is checked again")

    def test_a_revoked_signer_clears_the_cache_so_the_next_order_rechecks(self):
        self.ns["armed"] = True
        self.write("/api/order", self.order())
        self.trader.submit = lambda action, count=1, **_: {
            "outcome": "rejected", "action": action, "rows": [],
            "error": "Exchange rejected the action: User or API Wallet 0xabc does not exist."}
        self.write("/api/order", self.order())
        self.assertEqual(self.ns["_agent_checked"]["signer"], None)

    def test_a_failed_signer_check_never_submits_and_is_not_cached(self):
        from hyperliquid_client import HyperliquidError
        self.ns["armed"] = True
        self.ns["hyperliquid"].require_agent.side_effect = HyperliquidError("not approved")
        with self.assertRaises(HyperliquidError):
            self.write("/api/order", self.order())
        self.assertEqual(self.trader.calls, [])
        self.assertEqual(self.ns["_agent_checked"]["signer"], None)

    def test_a_prefetched_signer_check_is_used_instead_of_a_second_lookup(self):
        self.ns["armed"] = True
        result = self.ns["hyperliquid_write"]("/api/order", self.order(), prefetched={"agent": done()})
        self.assertEqual(result["outcome"], "confirmed")
        self.ns["hyperliquid"].require_agent.assert_not_called()

    def test_a_prefetched_position_read_feeds_the_close_check(self):
        calls = []

        def info(kind, **params):
            calls.append(kind)
            if kind == "extraAgents":
                return [{"address": SIGNER, "validUntil": 9999999999999}]
            raise AssertionError(f"{kind} must come from the prefetch, not a second read")
        backend = HyperliquidBackend(Mock(), client=SimpleNamespace(info=info), network="testnet",
                                     account_address=ACCOUNT, enable_feed=False)
        backend.markets = lambda: {s: {"instrument": {**i, "tradeable": True}} for s, i in MARKETS.items()}
        trader = make_trader(response={"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"oid": 1, "totalSz": "2", "avgPx": "0.6"}}]}}})
        self.ns.update(hyperliquid=backend, armed=True, _hl_trader={"ready": True, "trader": trader, "reason": None})
        current = [{"symbol": "HL_APT", "side": "short", "size": 2, "sizeExact": "2", "price": 0.8}]
        body = self.order(orderType="ioc", reduceOnly=True, closePosition=True, size=2, limitPrice=0.6)
        result = self.ns["hyperliquid_write"]("/api/order", body, prefetched={"positions": done(current)})
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(calls, ["extraAgents"])
        # The check itself still runs: a changed position refuses the close.
        from hyperliquid_client import HyperliquidError
        with self.assertRaises(HyperliquidError):
            self.ns["hyperliquid_write"]("/api/order", body, prefetched={"positions": done([])})


class StreamedBookTests(unittest.TestCase):
    def setUp(self):
        self.info_calls = []
        self.published = []

        def info(kind, **params):
            self.info_calls.append(kind)
            return {"coin": "APT", "time": time.time() * 1000,
                    "levels": [[{"px": "0.59", "sz": "10", "n": 1}], [{"px": "0.61", "sz": "10", "n": 1}]]}
        self.backend = HyperliquidBackend(lambda kind, payload: self.published.append((kind, payload)),
                                          client=SimpleNamespace(info=info), network="testnet",
                                          account_address=ACCOUNT, enable_feed=False)
        self.backend.markets = lambda: {"HL_APT": {"instrument": {"coin": "APT", "tradeable": True}, "ticker": {}}}
        self.backend._coins = {"APT": "HL_APT"}
        self.backend._tickers = {"HL_APT": {"symbol": "HL_APT"}}

    def push(self, bid, ask, *, stamp=None):
        self.backend.on_message({"channel": "l2Book", "data": {"coin": "APT", "time": stamp or time.time() * 1000,
            "levels": [[{"px": str(bid), "sz": "3", "n": 1}, {"px": str(bid - 0.01), "sz": "5", "n": 1}],
                       [{"px": str(ask), "sz": "4", "n": 1}]]}})

    def test_a_live_streamed_book_serves_orders_without_a_rest_call(self):
        self.push(0.6, 0.62)
        book = self.backend.orderbook("HL_APT", fresh=True)
        self.assertEqual(book["orderBook"]["bids"][0], (0.6, 3.0))
        self.assertEqual(book["orderBook"]["asks"][0], (0.62, 4.0))
        self.assertEqual(self.info_calls, [], "the ~250 ms REST book fetch is skipped")
        self.assertEqual(self.published[-1][1]["bid"], 0.6, "the quote reaches the screen live")

    def test_a_stale_stream_falls_back_to_rest(self):
        self.push(0.6, 0.62)
        coin, (received, book) = next(iter(self.backend._books.items()))
        self.backend._books[coin] = (received - 2.0, book)
        self.assertEqual(self.backend.orderbook("HL_APT", fresh=True)["orderBook"]["bids"][0], (0.59, 10.0))
        self.assertEqual(self.info_calls, ["l2Book"])

    def test_an_old_exchange_stamp_is_not_trusted_even_if_just_received(self):
        self.push(0.6, 0.62, stamp=time.time() * 1000 - 10_000)
        self.backend.orderbook("HL_APT", fresh=True)
        self.assertEqual(self.info_calls, ["l2Book"])

    def test_a_crossed_streamed_book_is_ignored(self):
        self.push(0.6, 0.62)
        self.push(0.7, 0.65)  # crossed: rejected, the last good book stays
        self.assertEqual(self.backend.orderbook("HL_APT", fresh=True)["orderBook"]["bids"][0], (0.6, 3.0))
        self.assertNotIn("status", [kind for kind, _ in self.published], "one bad frame is not a feed outage")

    def test_an_unchanged_quote_is_not_republished(self):
        self.push(0.6, 0.62)
        before = len(self.published)
        self.push(0.6, 0.62)
        self.assertEqual(len([p for p in self.published[before:] if p[0] == "ticker"]), 0)


if __name__ == "__main__":
    unittest.main()


class BboQuoteTests(StreamedBookTests):
    """Market orders price off the bbo stream: several pushes a second, top of book."""

    def bbo(self, bid, ask, *, stamp=None, sides=None):
        levels = sides if sides is not None else [{"px": str(bid), "sz": "2", "n": 1}, {"px": str(ask), "sz": "3", "n": 1}]
        self.backend.on_message({"channel": "bbo", "data": {"coin": "APT", "time": stamp or time.time() * 1000,
                                                            "bbo": levels}})

    def test_a_live_bbo_prices_the_order_without_any_rest_call(self):
        self.bbo(0.6, 0.62)
        quote = self.backend.quote("HL_APT")
        self.assertEqual(quote["orderBook"], {"bids": [(0.6, 2.0)], "asks": [(0.62, 3.0)]})
        self.assertEqual(quote["source"], "bbo")
        self.assertEqual(self.info_calls, [])

    def test_a_stale_or_skewed_bbo_falls_back_to_a_fresh_book(self):
        self.bbo(0.6, 0.62)
        coin, (received, top) = next(iter(self.backend._bbo.items()))
        self.backend._bbo[coin] = (received - 2.0, top)
        self.assertEqual(self.backend.quote("HL_APT")["orderBook"]["bids"][0], (0.59, 10.0))
        self.assertEqual(self.info_calls, ["l2Book"])
        self.bbo(0.6, 0.62, stamp=time.time() * 1000 - 6000)
        self.backend.quote("HL_APT")
        self.assertEqual(self.info_calls, ["l2Book", "l2Book"], "a frozen upstream is not trusted")

    def test_a_clock_a_second_off_still_uses_the_stream(self):
        self.bbo(0.6, 0.62, stamp=time.time() * 1000 - 1300)  # this PC runs ~1.2 s ahead of Hyperliquid
        self.assertEqual(self.backend.quote("HL_APT")["source"], "bbo")

    def test_one_sided_or_crossed_bbo_frames_are_skipped_quietly(self):
        self.bbo(0.6, 0.62)
        self.bbo(0, 0, sides=[None, {"px": "0.63", "sz": "1", "n": 1}])
        self.bbo(0.7, 0.65)
        self.assertEqual(self.backend.quote("HL_APT")["orderBook"]["bids"], [(0.6, 2.0)])
        self.assertNotIn("status", [kind for kind, _ in self.published])
