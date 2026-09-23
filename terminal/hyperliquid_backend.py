"""Hyperliquid native-perp views. Never imports Kraken, execution, or persistence code."""

import os
import re
import threading
import time
from copy import deepcopy
from decimal import Decimal

from hyperliquid_client import (ADDRESS_RE, RESOLUTIONS, HyperliquidClient, HyperliquidError,
                               candle_row, iso_time, number, optional_number, rows, symbol_for, trade_row)
from hyperliquid_feed import HyperliquidFeed
from hyperliquid_fills import decimal_text

READ_ONLY_MESSAGE = "Hyperliquid is read-only. Signed trading, Grid, Chase, and AI execution are not enabled."


# A streamed book received longer ago than this is not used for an order's price bound.
STREAM_BOOK_MAX_AGE = 1.5
# The exchange stamp only guards against a frozen upstream. It is compared with the
# local clock, which can run a second or more off, so its window is wider.
STREAM_STAMP_TOLERANCE_MS = 5000

class HyperliquidBackend:
    def __init__(self, publish, *, client=None, account_address=None, network=None, enable_feed=True):
        self.publish = publish
        self._cache_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._cache = {}
        self._views = {}
        self._markets = None
        self._tickers = {}
        self._books = {}  # coin -> (monotonic receipt, parsed book) from the l2Book stream
        self._bbo = {}    # coin -> (monotonic receipt, top-of-book) from the bbo stream
        self._coins = {}
        self._watch = set()
        self._config_error = None
        self.network = network if network is not None else os.getenv("HYPERLIQUID_NETWORK", "mainnet")
        try:
            self.client = client or HyperliquidClient(self.network)
        except HyperliquidError as exc:
            self.client = None
            self._config_error = str(exc)
        self.account_address = (account_address if account_address is not None else
                                os.getenv("HYPERLIQUID_ACCOUNT_ADDRESS", "")).strip()
        self.account_configured = bool(ADDRESS_RE.fullmatch(self.account_address)) and int(self.account_address[2:], 16) != 0
        self.feed = HyperliquidFeed(self.client.host, self.on_message) if enable_feed and self.client else None

    def health(self):
        # Deliberately no "armed" or "readOnly" here: ARM is process state this venue
        # backend does not own, and asserting it would let the UI show DISARMED while
        # writes are live. The server composes those into the response.
        return {"ok": not self._config_error,
                "env": "demo" if self.network == "testnet" else "live", "network": self.network,
                "hasKeys": False, "accountConfigured": self.account_configured,
                "hub": self.feed.status() if self.feed else {"status": "idle"},
                "message": self._config_error or READ_ONLY_MESSAGE}

    def _cached(self, key, ttl, load):
        # ponytail: serialize cache fills; use per-key locks if read contention matters.
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
            value = load()
            self._cache.pop(key, None)
            self._cache[key] = (time.monotonic(), value)
            if len(self._cache) > 128:
                self._cache.pop(next(iter(self._cache)))
            return value

    def _load_markets(self):
        payload = self.client.info("metaAndAssetCtxs")
        if not isinstance(payload, list) or len(payload) != 2 or not isinstance(payload[0], dict):
            raise HyperliquidError("Invalid Hyperliquid market catalog")
        universe, contexts = rows(payload[0].get("universe")), rows(payload[1])
        if not universe or len(universe) != len(contexts):
            raise HyperliquidError("Hyperliquid market catalog and contexts do not align")
        markets = {}
        for index, (asset, ctx) in enumerate(zip(universe, contexts)):
            coin = asset.get("name")
            symbol = symbol_for(coin)
            if symbol in markets:
                raise HyperliquidError("Ambiguous Hyperliquid symbol mapping")
            decimals = asset.get("szDecimals")
            if type(decimals) is not int or not 0 <= decimals <= 6:
                raise HyperliquidError("Invalid Hyperliquid size precision")
            mark = number(ctx.get("markPx"), minimum=0)
            # HL permits five significant figures, bounded decimal places, and
            # integer prices of any magnitude. This is display formatting only.
            price_decimals = min(6 - decimals, max(0, 4 - Decimal(str(mark)).adjusted())) if mark else 6 - decimals
            instrument = {"symbol": symbol, "coin": coin, "assetId": index, "exchange": "hyperliquid",
                          "pair": f"{coin}/USDC", "tradeable": asset.get("isDelisted") is not True,
                          "type": "futures_vanilla", "contractSize": 1, "contractValueTradePrecision": decimals,
                          "tickSize": float(Decimal(10) ** -price_decimals),
                          "maxLeverage": number(asset.get("maxLeverage"), positive=True)}
            markets[symbol] = {"instrument": instrument, "ticker": self._context(symbol, ctx)}
        return markets

    @staticmethod
    def _context(symbol, ctx):
        if not isinstance(ctx, dict):
            raise HyperliquidError("Invalid Hyperliquid asset context")
        mark = number(ctx.get("markPx"), minimum=0)
        previous = number(ctx.get("prevDayPx"), minimum=0)
        return {"symbol": symbol, "exchange": "hyperliquid", "markPrice": mark,
                "indexPrice": number(ctx.get("oraclePx"), minimum=0),
                "vol24h": optional_number(ctx.get("dayBaseVlm")),
                "volumeQuote": number(ctx.get("dayNtlVlm"), minimum=0),
                "fundingRate": number(ctx.get("funding")),
                "openInterest": number(ctx.get("openInterest"), minimum=0),
                "change24h": (mark / previous - 1) * 100 if previous else None,
                "changeBasis": "mark", "time": time.time()}

    def markets(self):
        if self._config_error:
            raise HyperliquidError(self._config_error)
        markets = self._cached("markets", 10, self._load_markets)
        with self._state_lock:
            if self._markets is not markets:
                self._markets = markets
                self._coins = {entry["instrument"]["coin"]: symbol for symbol, entry in markets.items()}
                for symbol, entry in markets.items():
                    self._tickers[symbol] = {"last": None, **self._tickers.get(symbol, {}), **entry["ticker"]}
        return markets

    def coin(self, symbol):
        entry = self.markets().get(symbol)
        if not entry:
            raise HyperliquidError("Unknown Hyperliquid native-perp symbol")
        return entry["instrument"]["coin"]

    def watch(self, symbols):
        coins = [self.coin(symbol) for symbol in symbols]
        if self.feed:
            self.feed.watch(coins)
        with self._state_lock:
            self._watch.update(symbols)

    def _apply_trades(self, raw, coin):
        trades = sorted((trade_row(row, coin) for row in rows(raw)), key=lambda t: (t["time"], int(t["id"])))
        for trade in trades:
            with self._state_lock:
                ticker = self._tickers.get(trade["symbol"], {"symbol": trade["symbol"], "exchange": "hyperliquid"})
                if (trade["time"], int(trade["id"])) <= (ticker.get("lastTime", 0), int(ticker.get("lastTradeId", -1))):
                    continue
                ticker = {**ticker, "last": trade["price"], "lastTime": trade["time"], "lastTradeId": trade["id"]}
                self._tickers[trade["symbol"]] = ticker
            self.publish("ticker", ticker)
            self.publish("trade", trade)
        return trades

    def trades(self, symbol):
        coin = self.coin(symbol)
        return self._cached(f"trades:{coin}", 5, lambda: self._apply_trades(self.client.info("recentTrades", coin=coin), coin))

    def tickers(self, symbols):
        self.markets()
        self.watch(symbols)
        for symbol in symbols:
            self.trades(symbol)
        with self._state_lock:
            return {"tickers": deepcopy(self._tickers), "watchlist": sorted(self._watch)}

    def candles(self, symbol, resolution):
        if resolution not in RESOLUTIONS:
            raise HyperliquidError("Unsupported candle resolution")
        coin = self.coin(symbol)
        self.watch([symbol])

        def load():
            end = int(time.time() * 1000)
            raw = self.client.info("candleSnapshot", req={"coin": coin, "interval": resolution,
                                   "startTime": end - 1500 * RESOLUTIONS[resolution] * 1000, "endTime": end})
            candles = sorted([candle_row(row, coin, resolution) for row in rows(raw)])
            if not candles or len({row[0] for row in candles}) != len(candles):
                raise HyperliquidError("Hyperliquid candle history is empty or duplicated")
            return {"symbol": symbol, "resolution": resolution, "source": "hyperliquid", "candles": candles}
        return self._cached(f"candles:{coin}:{resolution}", 5, load)

    def _parse_book(self, raw, coin):
        if not isinstance(raw, dict) or raw.get("coin") != coin or not isinstance(raw.get("levels"), list) or len(raw["levels"]) != 2:
            raise HyperliquidError("Invalid Hyperliquid order book")
        bids, asks = [[(number(level.get("px"), positive=True), number(level.get("sz"), minimum=0))
                       for level in rows(side)] for side in raw["levels"]]
        bids, asks = sorted(bids, reverse=True), sorted(asks)
        if not bids or not asks or bids[0][0] >= asks[0][0]:
            raise HyperliquidError("Hyperliquid order book is empty or crossed")
        return {"orderBook": {"bids": bids, "asks": asks}, "time": number(raw.get("time"), positive=True)}

    def _publish_quote(self, symbol, book):
        bid, ask = book["orderBook"]["bids"][0][0], book["orderBook"]["asks"][0][0]
        with self._state_lock:
            previous = self._tickers.get(symbol, {})
            if previous.get("bid") == bid and previous.get("ask") == ask:
                return
            ticker = {**previous, "bid": bid, "ask": ask}
            self._tickers[symbol] = ticker
        self.publish("ticker", ticker)

    def streamed_book(self, coin, max_age=STREAM_BOOK_MAX_AGE):
        """The live book if it arrived within max_age seconds and its exchange stamp is
        current. None means use REST."""
        with self._state_lock:
            entry = self._books.get(coin)
        if not entry or time.monotonic() - entry[0] > max_age:
            return None
        if abs(time.time() * 1000 - entry[1]["time"]) > STREAM_STAMP_TOLERANCE_MS:
            return None
        return deepcopy(entry[1])

    def quote(self, symbol):
        """Best bid and ask for pricing a market order or a Chase peg: the live bbo
        stream when fresh, otherwise a fresh full book. Book-shaped, top level only."""
        coin = self.coin(symbol)
        if self.feed:
            try:
                self.watch([symbol])
            except ValueError:
                pass
        with self._state_lock:
            entry = self._bbo.get(coin)
        if (entry and time.monotonic() - entry[0] <= STREAM_BOOK_MAX_AGE
                and abs(time.time() * 1000 - entry[1]["time"]) <= STREAM_STAMP_TOLERANCE_MS):
            top = entry[1]
            return {"orderBook": {"bids": [top["bid"]], "asks": [top["ask"]]}, "time": top["time"], "source": "bbo"}
        return self.orderbook(symbol, fresh=True)

    def orderbook(self, symbol, *, fresh=False):
        coin = self.coin(symbol)
        # Keep streaming this market so the next read is already live.
        if self.feed:
            try:
                self.watch([symbol])
            except ValueError:
                pass
        streamed = self.streamed_book(coin, STREAM_BOOK_MAX_AGE if fresh else 3.0)
        if streamed is not None:
            return streamed

        def load():
            book = self._parse_book(self.client.info("l2Book", coin=coin), coin)
            self._publish_quote(symbol, book)
            return book
        return load() if fresh else self._cached(f"book:{coin}", 2, load)

    def trading_capacity(self, symbol):
        coin = self.coin(symbol)
        if not self.account_configured:
            raise HyperliquidError("Hyperliquid account is not configured")
        data = self.client.info("activeAssetData", user=self.account_address, coin=coin)
        if (not isinstance(data, dict) or data.get("coin") != coin or
                str(data.get("user", "")).lower() != self.account_address.lower()):
            raise HyperliquidError("Trading capacity account or coin mismatch")
        leverage = data.get("leverage")
        maximum = self.markets()[symbol]["instrument"]["maxLeverage"]
        if (not isinstance(leverage, dict) or leverage.get("type") not in {"cross", "isolated"} or
                type(leverage.get("value")) is not int or not 1 <= leverage["value"] <= maximum):
            raise HyperliquidError("Invalid exchange leverage setting")
        sizes, available = data.get("maxTradeSzs"), data.get("availableToTrade")
        if not isinstance(sizes, list) or len(sizes) != 2 or not isinstance(available, list) or len(available) != 2:
            raise HyperliquidError("Invalid directional trading capacity")
        for value in sizes + available:
            number(value, minimum=0)
        return {"symbol": symbol, "network": self.network, "accountAddress": self.account_address,
                "leverage": {"type": leverage["type"], "value": leverage["value"]},
                "maxTradeSizes": dict(zip(("buy", "sell"), map(decimal_text, sizes))),
                "availableToTrade": dict(zip(("buy", "sell"), map(decimal_text, available))),
                "markPrice": number(data.get("markPx"), positive=True), "receivedAt": int(time.time() * 1000)}

    def require_agent(self, signer):
        """Fresh account binding check. This only reads /info; it never approves a key."""
        if not self.account_configured or not isinstance(signer, str) or not ADDRESS_RE.fullmatch(signer):
            raise HyperliquidError("Hyperliquid account or API signer identity is unavailable")
        agents = rows(self.client.info("extraAgents", user=self.account_address))
        matches = [agent for agent in agents if isinstance(agent.get("address"), str)
                   and agent["address"].lower() == signer.lower()]
        if (len(matches) != 1 or type(matches[0].get("validUntil")) is not int or
                matches[0]["validUntil"] <= int(time.time() * 1000)):
            raise HyperliquidError("Configured API signer is not currently approved for this Hyperliquid account")

    def _user_info(self, kind, *, fresh=False):
        if not self.account_configured:
            raise HyperliquidError("Set HYPERLIQUID_ACCOUNT_ADDRESS to the master or subaccount address for read-only account data")
        if fresh:
            return self.client.info(kind, user=self.account_address)
        return self._cached(kind, 5, lambda: self.client.info(kind, user=self.account_address))

    def _account_state(self, *, fresh=False):
        state = self._user_info("clearinghouseState", fresh=fresh)
        if not isinstance(state, dict) or not isinstance(state.get("marginSummary"), dict):
            raise HyperliquidError("Invalid Hyperliquid account state")
        rows(state.get("assetPositions"))
        return state

    def abstraction(self):
        """Account mode. Unified modes keep one USDC balance shared by spot and perps.

        Docs: "for API users, unified account and portfolio margin show all balances
        and holds in the spot clearinghouse state, and individual perp dex user states
        are not meaningful". So the perp figure must not be shown as the account balance.
        """
        try:
            value = self._user_info("userAbstraction")
        except HyperliquidError:
            return None
        return value if isinstance(value, str) else None

    def account(self):
        state = self._account_state()
        margin = state["marginSummary"]
        mode = self.abstraction()
        unified = mode in {"unifiedAccount", "portfolioMargin"}
        balances, usdc = {}, None
        try:
            for entry in rows(self._user_info("spotClearinghouseState").get("balances", [])):
                coin = entry.get("coin")
                if isinstance(coin, str) and coin:
                    balances[coin] = number(entry.get("total", "0"), minimum=0)
            usdc = balances.get("USDC", 0.0)
        except HyperliquidError:
            # A failed read stays unknown. A successful read with no USDC IS zero.
            balances = {}
        # In a unified account this single balance is the perp collateral too, so it is
        # the account's balance. None stays None when the read failed rather than 0.
        balance_value = usdc if unified else number(margin.get("totalRawUsd"))
        return {"id": "hyperliquid-native-perps", "type": "perpetual", "currency": "USDC",
                "mode": mode, "unified": unified,
                "balanceValue": balance_value,
                "balanceBasis": "unified" if unified else "perp",
                "usdc": usdc, "spotUsdc": usdc, "balances": balances,
                "portfolioValue": number(margin.get("accountValue")),
                "initialMargin": number(margin.get("totalMarginUsed"), minimum=0),
                # Withdrawable is not an order-sizing or available-margin estimate, and
                # the perp figure is not meaningful in a unified account.
                "availableMargin": None,
                "withdrawable": None if unified else number(state.get("withdrawable"), minimum=0)}

    def positions(self, *, fresh=False):
        positions = []
        for row in self._account_state(fresh=fresh)["assetPositions"]:
            position = row.get("position")
            if not isinstance(position, dict):
                raise HyperliquidError("Invalid Hyperliquid position")
            symbol = symbol_for(position.get("coin"))
            self.coin(symbol)
            size = number(position.get("szi"))
            if not size:
                continue
            funding = position.get("cumFunding")
            if funding is not None and not isinstance(funding, dict):
                raise HyperliquidError("Invalid Hyperliquid position funding")
            positions.append({"symbol": symbol, "exchange": "hyperliquid", "size": abs(size),
                              "sizeExact": decimal_text(position.get("szi")).lstrip("-"),
                              "side": "long" if size > 0 else "short", "price": number(position.get("entryPx"), positive=True),
                              "unrealizedPnl": number(position.get("unrealizedPnl")),
                              "liqPriceEstimate": optional_number(position.get("liquidationPx")), "liquidationSource": "exchange",
                              "fundingSinceOpen": optional_number((funding or {}).get("sinceOpen"))})
        self.watch([position["symbol"] for position in positions])
        return {"positions": positions}

    def validate_close(self, symbol, side, size, expected=None, *, positions=None):
        # positions: a fresh read the caller already started in parallel with its other checks.
        current = positions if positions is not None else self.positions(fresh=True)["positions"]
        matches = [p for p in current if p["symbol"] == symbol]
        if len(matches) != 1:
            raise HyperliquidError("Close requires one current open position; refresh positions")
        position = matches[0]
        if expected is not None:
            if (not isinstance(expected, dict) or expected.get('side') != position['side'] or
                    decimal_text(expected.get('sizeExact'), positive=True) != decimal_text(position['sizeExact'], positive=True) or
                    decimal_text(expected.get('price'), positive=True) != decimal_text(position['price'], positive=True) or
                    Decimal(decimal_text(size, positive=True)) != Decimal(position['sizeExact'])):
                raise HyperliquidError('Reviewed position changed. Refresh before closing')
        opposite = "sell" if position["side"] == "long" else "buy"
        if side != opposite or Decimal(decimal_text(size, positive=True)) > Decimal(position["sizeExact"]):
            raise HyperliquidError("Position changed or close size exceeds current exposure; refresh positions")

    def _native_symbol(self, coin):
        if isinstance(coin, str) and (coin.startswith("@") or "/" in coin or ":" in coin):
            return None  # Spot and HIP-3 are outside this native-perps view.
        symbol = symbol_for(coin)
        self.coin(symbol)
        return symbol

    def orders(self, *, fresh=False):
        # Resolve the account first so an unconfigured wallet fails before any market read.
        raw = rows(self._user_info("frontendOpenOrders", fresh=fresh))
        markets = self.markets() if raw else {}
        result = []
        for order in raw:
            symbol = self._native_symbol(order.get("coin"))
            if symbol is None:
                continue
            if order.get("side") not in {"A", "B"} or any(type(order.get(key)) is not bool for key in ("reduceOnly", "isTrigger")):
                raise HyperliquidError("Invalid Hyperliquid order side or flags")
            oid = order.get("oid")
            if type(oid) is not int or oid < 0:
                raise HyperliquidError("Invalid Hyperliquid order identity")
            remaining = number(order.get("sz"), minimum=0)
            original = number(order.get("origSz"), minimum=remaining)
            kind = order.get("orderType")
            if not isinstance(kind, str) or not kind.strip():
                raise HyperliquidError("Missing Hyperliquid order type")
            kind = kind.lower()
            order_type = "take_profit" if kind.startswith("take profit") else "stp" if kind.startswith("stop") else "lmt" if kind == "limit" else kind
            # Trigger nature is preserved verbatim by chart amendments, so it is reported
            # explicitly instead of being re-derived from a display string.
            trigger_kind = ("tp" if kind.startswith("take profit") else "sl" if kind.startswith("stop") else None) \
                if order["isTrigger"] else None
            result.append({"symbol": symbol, "exchange": "hyperliquid", "asset": markets[symbol]["instrument"]["assetId"],
                           "order_id": str(oid), "cliOrdId": order.get("cloid"),
                           "side": "buy" if order["side"] == "B" else "sell", "orderType": order_type,
                           "size": original, "unfilledSize": remaining,
                           "positionTpsl": order["isTrigger"] and order["reduceOnly"] and original == 0 and
                               order.get("isPositionTpsl", True) is True,
                           "unfilledSizeExact": decimal_text(order.get("sz")),
                           "limitPrice": optional_number(order.get("limitPx")),
                           "stopPrice": number(order.get("triggerPx"), positive=True) if order["isTrigger"] else None,
                           "triggerKind": trigger_kind, "triggerMarket": (True if kind.endswith("market") else False if kind.endswith("limit") else None) if order["isTrigger"] else None,
                           "reduceOnly": order["reduceOnly"], "receivedTime": iso_time(order.get("timestamp"))})
        return {"orders": result, "scope": "native-perps"}

    def order_status(self, identifier):
        """Fresh read by exact oid/cloid. unknownOid is not proof of non-submission."""
        if not self.account_configured:
            raise HyperliquidError("Hyperliquid account is not configured")
        if not isinstance(identifier, str):
            raise HyperliquidError("An exact order id or client order id is required")
        if re.fullmatch(r"0x[0-9a-fA-F]{32}", identifier):
            query_id = identifier
        elif re.fullmatch(r"[0-9]{1,20}", identifier) and 0 < int(identifier) < 2**64:
            query_id = int(identifier)
        else:
            raise HyperliquidError("Invalid order id or client order id")
        raw = self.client.info("orderStatus", user=self.account_address, oid=query_id)
        if not isinstance(raw, dict):
            raise HyperliquidError("Invalid order status response")
        if raw.get("status") == "unknownOid":
            return {"found": False, "orderStatus": "unknownOid", "uncertain": True,
                    "requestedId": identifier}
        detail = raw.get("order")
        if raw.get("status") != "order" or not isinstance(detail, dict):
            raise HyperliquidError("Invalid order status response")
        order = detail.get("order")
        if not isinstance(order, dict):
            raise HyperliquidError("Missing order status identity")
        oid = order.get("oid")
        if type(oid) is not int or not 0 < oid < 2**64:
            raise HyperliquidError("Invalid order status identity")
        if ((isinstance(query_id, int) and oid != query_id) or
                (isinstance(query_id, str) and str(order.get("cloid", "")).lower() != query_id.lower())):
            raise HyperliquidError("Order status identity mismatch")
        status = detail.get("status")
        if not isinstance(status, str) or not status:
            raise HyperliquidError("Missing order status")
        symbol = symbol_for(order.get("coin"))
        if order.get("side") not in {"A", "B"} or type(order.get("reduceOnly")) is not bool:
            raise HyperliquidError("Invalid order status side or reduce-only flag")
        remaining = number(order.get("sz"), minimum=0)
        return {"found": True, "requestedId": identifier, "orderStatus": status,
                "order_id": str(oid), "cliOrdId": order.get("cloid"), "symbol": symbol,
                "side": "buy" if order["side"] == "B" else "sell", "reduceOnly": order["reduceOnly"],
                "isTrigger": order.get("isTrigger") is True,
                "triggerPrice": number(order.get("triggerPx"), positive=True) if order.get("isTrigger") is True else None,
                "originalSize": number(order.get("origSz"), minimum=remaining),
                "originalSizeExact": decimal_text(order.get("origSz")),
                "positionTpsl": order.get("isTrigger") is True and order["reduceOnly"] and
                    number(order.get("origSz"), minimum=remaining) == 0 and order.get("isPositionTpsl", True) is True,
                "remainingSizeExact": decimal_text(order.get("sz")),
                "remainingSize": remaining, "statusTime": iso_time(detail.get("statusTimestamp"))}

    def fills(self):
        result = []
        for fill in rows(self._user_info("userFills")):
            symbol = self._native_symbol(fill.get("coin"))
            if symbol is None:
                continue
            trade = trade_row(fill, fill["coin"])
            oid = fill.get("oid")
            if type(oid) is not int or not 0 < oid < 2**64:
                raise HyperliquidError("Invalid Hyperliquid fill order identity")
            result.append({"symbol": symbol, "exchange": "hyperliquid", "side": trade["side"],
                           "price": trade["price"], "size": trade["qty"], "fillTime": iso_time(fill["time"]),
                           "id": trade["id"], "order_id": str(oid),
                           "fee": optional_number(fill.get("fee")), "feeToken": fill.get("feeToken"),
                           "realizedPnl": optional_number(fill.get("closedPnl"))})
        return {"fills": result, "scope": "native-perps", "history": "recent-only"}

    def read(self, path, query):
        if path == "/api/health":
            return self.health(), 200
        symbol = str(query.get("symbol", "")).upper()
        loaders = {
            "/api/instruments": lambda: {"instruments": [row["instrument"] for row in self.markets().values()]},
            "/api/tickers": lambda: self.tickers([s.upper() for s in query.get("symbols", "").split(",") if s]),
            "/api/candles": lambda: self.candles(symbol, query.get("res", "1m")),
            "/api/orderbook": lambda: self.orderbook(symbol),
            "/api/trades": lambda: {"trades": self.trades(symbol)[-min(500, max(1, int(query.get("limit", "50")))):]},
            "/api/account": self.account, "/api/positions": lambda: self.positions(fresh=query.get('fresh') == '1'),
            "/api/orders": self.orders, "/api/fills": self.fills,
            "/api/marketlist": self.marketlist,
            "/api/order-status": lambda: self.order_status(query.get("id")),
            "/api/trading-capacity": lambda: self.trading_capacity(symbol),
        }
        if path not in loaders:
            return {"state": "unsupported", "error": f"{path} is not available for Hyperliquid. No Kraken fallback."}, 501
        key = (path, tuple(sorted(query.items())))
        try:
            if self._config_error:
                raise HyperliquidError(self._config_error)
            result = {**loaders[path](), "state": "current", "exchange": "hyperliquid"}
            with self._state_lock:
                self._views.pop(key, None)
                self._views[key] = (time.monotonic(), deepcopy(result))
                if len(self._views) > 128:
                    self._views.pop(next(iter(self._views)))
            return result, 200
        except (HyperliquidError, ValueError, TypeError, KeyError, OverflowError) as exc:
            with self._state_lock:
                previous = self._views.get(key)
                result = deepcopy(previous[1]) if previous else {}
            # Read failures preserve this venue's last-known view, never another venue's data.
            result.update(state="unavailable", error=str(exc), exchange="hyperliquid",
                          ageSeconds=round(time.monotonic() - previous[0], 1) if previous else None)
            return result, 200 if path in {"/api/account", "/api/positions", "/api/orders", "/api/fills"} else 503

    def marketlist(self):
        self.markets()
        with self._state_lock:
            return {"rows": [{**self._tickers[symbol], "vol24h": self._tickers[symbol]["volumeQuote"], "priceBasis": "mark"}
                             for symbol, row in self._markets.items() if row["instrument"]["tradeable"]]}

    def watchlist(self):
        with self._state_lock:
            return sorted(self._watch)

    def on_message(self, message):
        if not isinstance(message, dict):
            return
        channel, data = message.get("channel"), message.get("data")
        try:
            if channel == "status":
                self.publish("status", data)
            elif channel == "trades":
                for trade in rows(data):
                    if trade.get("coin") in self._coins:
                        self._apply_trades([trade], trade["coin"])
            elif channel == "activeAssetCtx" and isinstance(data, dict) and data.get("coin") in self._coins:
                symbol = self._coins[data["coin"]]
                ctx = self._context(symbol, data.get("ctx"))
                with self._state_lock:
                    ticker = {"last": None, **self._tickers.get(symbol, {}), **ctx}
                    self._tickers[symbol] = ticker
                self.publish("ticker", ticker)
            elif channel == "l2Book" and isinstance(data, dict) and data.get("coin") in self._coins:
                try:
                    book = self._parse_book(data, data["coin"])
                except HyperliquidError:
                    return  # a crossed or empty frame: keep the last good book, the feed is fine
                with self._state_lock:
                    self._books[data["coin"]] = (time.monotonic(), book)
                self._publish_quote(self._coins[data["coin"]], book)
            elif channel == "bbo" and isinstance(data, dict) and data.get("coin") in self._coins:
                levels = data.get("bbo")
                if not isinstance(levels, list) or len(levels) != 2 or not all(isinstance(l, dict) for l in levels):
                    return  # one side empty: keep the last quote, the stale check retires it
                try:
                    bid = (number(levels[0].get("px"), positive=True), number(levels[0].get("sz"), minimum=0))
                    ask = (number(levels[1].get("px"), positive=True), number(levels[1].get("sz"), minimum=0))
                    stamp = number(data.get("time"), positive=True)
                except HyperliquidError:
                    return
                if not bid[0] < ask[0]:
                    return
                with self._state_lock:
                    self._bbo[data["coin"]] = (time.monotonic(), {"bid": bid, "ask": ask, "time": stamp})
                self._publish_quote(self._coins[data["coin"]], {"orderBook": {"bids": [bid], "asks": [ask]}})
            elif channel == "candle":
                for row in rows(data if isinstance(data, list) else [data]):
                    coin = row.get("s")
                    if coin not in self._coins:
                        continue
                    t, op, high, low, close, volume = candle_row(row, coin, "1m")
                    self.publish("bcandle", {"symbol": self._coins[coin], "source": "hyperliquid", "t": t,
                                             "o": op, "h": high, "l": low, "c": close, "v": volume})
            elif channel == "error":
                self.publish("status", {"status": "unavailable", "exchange": "hyperliquid"})
        except (HyperliquidError, TypeError, KeyError):
            self.publish("status", {"status": "unavailable", "exchange": "hyperliquid"})
