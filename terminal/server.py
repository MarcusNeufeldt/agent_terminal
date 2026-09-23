"""Kraken Futures trading terminal server.

Stdlib-only HTTP server (ThreadingHTTPServer) serving:
  - static frontend from ./static
  - REST API (public market data, private account data, guarded order entry)
  - SSE stream /api/stream fanning out live tickers, trades, and hub status
  - AI chat POST /api/chat (OpenRouter, live account/market context)

Order safety: every POST needs the per-process browser token and an exact local
Origin. POST /api/order is SIMULATED until /api/arm consumes a one-time signed
challenge. Armed state lives in memory only; restarting always disarms.
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import grid_move
from tp_cleanup import TakeProfitCleanup
from exchange_routing import ExchangeRouting, ExchangeRoutingError, requested_exchange
from hyperliquid_backend import HyperliquidBackend, READ_ONLY_MESSAGE
import hyperliquid_trading
import hyperliquid_recovery
import hyperliquid_fills
import hyperliquid_lifecycle
import hyperliquid_grid
import hyperliquid_chart

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from kraken_client import (  # noqa: E402
    KrakenFuturesClient,
    KrakenFuturesError,
    KrakenHTTPError,
    load_env_file,
)
from market_hub import DEFAULT_WATCHLIST, MarketHub  # noqa: E402
import ai_chat  # noqa: E402
import actions as trading_actions  # noqa: E402
import scanner  # noqa: E402
import chase as chase_mod  # noqa: E402
import db as db_mod  # noqa: E402
import account_log  # noqa: E402
import binance_candles  # noqa: E402
import alt_btc  # noqa: E402
import binance_ws as binance_ws_mod  # noqa: E402
import chat_compaction  # noqa: E402
from actions import ActionContext  # noqa: E402
from local_security import LocalSecurity, safe_static_path  # noqa: E402
from read_state import account_payload as _account_payload, rows_payload as _rows_payload  # noqa: E402

STATIC_DIR = HERE / "static"

# Load configuration before reading PORT or constructing clients.
load_env_file(HERE / ".env")
load_env_file(Path(r"F:\explore\kraken-futures-cli") / ".env")
PORT = int(os.getenv("PORT", "8787"))
VITE_ORIGINS = tuple(filter(None, (value.strip().lower() for value in os.getenv("VITE_DEV_ORIGINS", "").split(","))))
security = LocalSecurity(PORT, VITE_ORIGINS)
DEBUG_ENDPOINTS = os.getenv("TERMINAL_DEBUG", "").strip().lower() in {"1", "true", "yes"}
IDEMPOTENT_WRITE_PATHS = {"/api/order", "/api/leverage", "/api/chart-order", "/api/cancel", "/api/action", "/api/grid", "/api/flatten", "/api/chase", "/api/chase/abort", "/api/chat"}
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")

client = KrakenFuturesClient.from_env()
hub = MarketHub(client.base_url)

armed = False
arm_lock = threading.RLock()
exchange_routing = ExchangeRouting(arm_lock)


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._invalidated: set[str] = set()
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float) -> Any | None:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if key not in self._invalidated and hit and now - hit[0] < ttl:
                return hit[1]
        return None

    def peek(self, key: str) -> tuple[Any, float] | None:
        with self._lock:
            hit = self._data.get(key)
        return (hit[1], max(0.0, time.monotonic() - hit[0])) if hit else None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._invalidated.discard(key)

    def drop(self, *prefixes: str) -> None:
        with self._lock:
            self._invalidated.update(key for key in self._data if key.startswith(prefixes))


cache = TTLCache()


# ---- SSE fan-out -----------------------------------------------------------

class SseHub:
    def __init__(self) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((kind, payload))
            except queue.Full:
                pass


sse = SseHub()
hub.on_message = lambda kind, payload: sse.publish(kind, payload)
# Kraken client/context/DB remain permanently bound to Kraken. No global client swap.
hyperliquid_sse = SseHub()
hyperliquid = HyperliquidBackend(hyperliquid_sse.publish)
_hl_trader: dict[str, Any] = {"ready": False, "trader": None, "reason": None}
_hl_gate: dict[str, Any] = {}


# ---- hyperliquid signed writes ---------------------------------------------

HL_TIF = {"mkt": "ioc", "lmt": "gtc", "post": "alo", "ioc": "ioc"}
HL_TRIGGER = {"stp": "sl", "take_profit": "tp"}
HL_WRITE_PATHS = {"/api/order", "/api/leverage", "/api/chart-order", "/api/cancel", "/api/order-reconcile", "/api/cancel-reconcile", "/api/fill-history/sync", "/api/grid/preview", "/api/grid"}
# These belong to the process, not to a venue: ARM guards every write, so the challenge
# and the toggle must be reachable from whichever venue the browser is currently on.
VENUE_NEUTRAL_PATHS = {"/api/arm", "/api/arm/challenge"}


def _hl_instrument(symbol: str) -> dict[str, Any]:
    instrument = (hyperliquid.markets().get(symbol) or {}).get("instrument")
    if not instrument or instrument.get("tradeable") is False:
        raise hyperliquid_trading.HyperliquidError("Unknown or untradeable Hyperliquid symbol")
    return instrument


def hyperliquid_gate() -> dict[str, Any]:
    """Credential-free view of the signed-trading gate for health reporting.

    Deliberately never retains the private key: the response leaves this process.
    """
    if not _hl_gate:
        credentials = hyperliquid_trading.load_trading_credentials()
        _hl_gate.update(mode=credentials["mode"], reason=credentials.get("reason"),
                        signer=credentials.get("signer_address"), host=credentials.get("host"))
        if credentials["mode"] != "off" and credentials["mode"] != hyperliquid.network:
            _hl_gate.update(mode="off", reason="Hyperliquid read and signing networks disagree")
    return dict(_hl_gate)


def hyperliquid_trader() -> tuple[Any, str | None]:
    """Build the signed trader once, on first use. Never at import time."""
    if not _hl_trader["ready"]:
        gate = hyperliquid_gate()
        if gate["mode"] == "off":
            return None, gate.get("reason") or "Hyperliquid signed trading is off"
        def market(symbol: str) -> dict[str, Any]:
            return (hyperliquid.markets().get(symbol) or {}).get("instrument") or {}
        trader, reason = hyperliquid_trading.build_trader(market)
        _hl_trader.update(ready=True, trader=trader, reason=reason)
        db.log_event("hyperliquid_trader", {"signed": trader is not None, "reason": reason})
    return _hl_trader["trader"], _hl_trader["reason"]


def hyperliquid_leverage_intent(body):
    instrument = _hl_instrument(str(body.get("symbol") or "").strip().upper())
    leverage, previous, cross = body.get("leverage"), body.get("expectedLeverage"), body.get("cross")
    if (type(leverage) is not int or not 1 <= leverage <= instrument.get("maxLeverage", 0) or
            type(previous) is not int or previous < 1 or type(cross) is not bool or type(body.get("expectedArmed")) is not bool):
        raise hyperliquid_trading.HyperliquidError("Valid exchange leverage, previous setting and margin mode are required")
    return {"type": "leverageIntent", "action": {"type": "updateLeverage", "asset": instrument["assetId"],
            "isCross": cross, "leverage": leverage}, "expiresAfter": int(time.time() * 1000) + 30000}


def hyperliquid_order_action(body: dict[str, Any]) -> dict[str, Any]:
    symbol = str(body.get("symbol") or "").strip().upper()
    side = str(body.get("side") or "").strip().lower()
    order_type = str(body.get("orderType") or "lmt").strip().lower()
    size = _as_float(body.get("size"))
    reduce_only = body.get("reduceOnly", False)
    close_position = body.get("closePosition", False)
    if type(close_position) is not bool or (close_position and (order_type not in {"ioc", "mkt"} or reduce_only is not True)):
        raise hyperliquid_trading.HyperliquidError("closePosition requires a reduce-only IOC or market order")
    if close_position and order_type == "mkt" and (type(body.get('expectedArmed')) is not bool or not isinstance(body.get('position'), dict)):
        raise hyperliquid_trading.HyperliquidError('Market close requires the reviewed position and ARM state')
    if type(reduce_only) is not bool:
        raise hyperliquid_trading.HyperliquidError("reduceOnly must be a boolean")
    if side not in {"buy", "sell"}:
        raise hyperliquid_trading.HyperliquidError("side must be buy or sell")
    if isinstance(body.get('size'), bool) or size is None or size <= 0:
        raise hyperliquid_trading.HyperliquidError("size must be a positive number")
    instrument = _hl_instrument(symbol)
    if "quickPercent" in body:
        if reduce_only:
            positions = hyperliquid.positions(fresh=True)["positions"]
            matches = [p for p in positions if p.get("symbol") == symbol]
            if len(matches) != 1 or matches[0].get("side") != ("long" if side == "sell" else "short"):
                raise hyperliquid_trading.HyperliquidError("No current position for this percentage reduction")
            available = matches[0]["sizeExact"]
        else:
            capacity = hyperliquid.trading_capacity(symbol)
            if (type(body.get("expectedLeverage")) is not int or
                    body["expectedLeverage"] != capacity["leverage"]["value"] or
                    body.get("expectedMarginMode") != capacity["leverage"]["type"]):
                raise hyperliquid_trading.HyperliquidError("Exchange leverage changed. Refresh quick sizing before submitting")
            available = capacity["maxTradeSizes"][side]
        size = hyperliquid_trading.percent_size(available, body["quickPercent"], size, instrument["contractValueTradePrecision"])
    if order_type in HL_TRIGGER:
        stop = _as_float(body.get("stopPrice"))
        if stop is None or stop <= 0:
            raise hyperliquid_trading.HyperliquidError("stopPrice is required for trigger orders")
        if not reduce_only:
            raise hyperliquid_trading.HyperliquidError("Hyperliquid trigger orders must be reduce-only")
        market = body.get("triggerMarket", False)
        if type(market) is not bool:
            raise hyperliquid_trading.HyperliquidError("triggerMarket must be a boolean")
        trigger = {"kind": HL_TRIGGER[order_type], "triggerPx": stop, "market": market}
        action = hyperliquid_trading.order_action_for(instrument, side, size, _as_float(body.get("limitPrice")) or stop,
                                                   tif="gtc", reduce_only=True, trigger=trigger,
                                                   cloid=body.get("cloid"))
        if "maxNotional" in body:
            hyperliquid_trading.validate_order_notional(action, body["maxNotional"])
        return action
    if order_type not in HL_TIF:
        raise hyperliquid_trading.HyperliquidError(
            f"orderType must be one of {sorted(set(HL_TIF) | set(HL_TRIGGER))}")
    if order_type == "mkt":
        if "maxNotional" in body and body["maxNotional"] is None:
            raise hyperliquid_trading.HyperliquidError("Maximum notional must be positive")
        if body.get("limitPrice") is not None:
            raise hyperliquid_trading.HyperliquidError("Market price is derived from fresh quotes, not a supplied limit")
        return hyperliquid_trading.market_action_for(instrument, side, size,
            hyperliquid.orderbook(symbol, fresh=True), body.get("slippagePercent", 0.5),
            cloid=body.get("cloid"), reduce_only=reduce_only, maximum=body.get("maxNotional"))
    price = _as_float(body.get("limitPrice"))
    if price is None or price <= 0:
        raise hyperliquid_trading.HyperliquidError("limitPrice is required")
    action = hyperliquid_trading.order_action_for(instrument, side, size, price, tif=HL_TIF[order_type],
                                               reduce_only=reduce_only, cloid=body.get("cloid"))
    rounded_price = float(action["orders"][0]["p"])
    if close_position and ((side == "buy" and rounded_price > price) or (side == "sell" and rounded_price < price)):
        raise hyperliquid_trading.HyperliquidError("Price rounding would exceed the close price bound; use an exchange-precision price")
    if "maxNotional" in body:
        hyperliquid_trading.validate_order_notional(action, body["maxNotional"])
    return action


def hyperliquid_cancel_action(body: dict[str, Any]) -> dict[str, Any]:
    if "targets" in body:
        scope = hyperliquid_recovery.cancel_scope(body)
        return {"type": "cancel", "cancels": [{"a": _hl_instrument(symbol)["assetId"], "o": int(identifier)}
                                              for identifier, symbol in scope.items()]}
    if "orderIds" in body:
        identifiers = body["orderIds"]
        if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 100 or
                any(body.get(key) is not None for key in ("orderId", "cliOrdId", "asset"))):
            raise hyperliquid_trading.HyperliquidError("Bulk cancel requires 1 to 100 exact orderIds and one symbol")
        parsed_ids = []
        for identifier in identifiers:
            if (not isinstance(identifier, str) or not identifier.isascii() or not identifier.isdigit() or
                    not 1 <= len(identifier) <= 20 or not 0 < int(identifier) < 2**64):
                raise hyperliquid_trading.HyperliquidError("Bulk cancel requires exact decimal-string orderIds")
            parsed_ids.append(int(identifier))
        if len(set(parsed_ids)) != len(parsed_ids):
            raise hyperliquid_trading.HyperliquidError("Duplicate bulk cancel identity")
        asset = _hl_instrument(str(body.get("symbol") or "").upper())["assetId"]
        return {"type": "cancel", "cancels": [{"a": asset, "o": identifier} for identifier in parsed_ids]}
    cloid = str(body.get("cliOrdId") or "")
    if cloid:
        if not hyperliquid_trading.CLOID_RE.fullmatch(cloid):
            raise hyperliquid_trading.HyperliquidError("Invalid client order id")
        return {"type": "cancelByCloid",
                "cancels": [{"asset": _hl_instrument(str(body.get("symbol") or "").upper())["assetId"],
                             "cloid": cloid}]}
    asset, oid = body.get("asset"), body.get("orderId")
    if body.get("symbol"):
        expected_asset = _hl_instrument(str(body["symbol"]).upper())["assetId"]
        if asset is not None and (type(asset) is not int or asset != expected_asset):
            raise hyperliquid_trading.HyperliquidError("asset does not match symbol")
        asset = expected_asset
    # Order ids are 64-bit and lose precision as JSON numbers in a browser, so a
    # decimal string is the transport-safe form. Python ints are unbounded.
    if isinstance(oid, str) and oid.isascii() and oid.isdigit() and len(oid) <= 20:
        oid = int(oid)
    if type(asset) is not int or type(oid) is not int or asset < 0 or not 0 < oid < 2**64:
        raise hyperliquid_trading.HyperliquidError(
            "cancel requires cliOrdId with a symbol, or integer asset and orderId")
    return {"type": "cancel", "cancels": [{"a": asset, "o": oid}]}


def hyperliquid_write(path: str, body: dict[str, Any], *, prepared_action=None) -> dict[str, Any]:
    """Validate, then simulate or sign. Mirrors the Kraken DISARMED contract."""
    expires_after = None
    if path == "/api/chart-order":
        intent = prepared_action if prepared_action is not None else hyperliquid_chart.prepare(body, hyperliquid)
        action, expires_after = intent["action"], intent["expiresAfter"]
    elif path == "/api/leverage":
        intent = prepared_action if prepared_action is not None else hyperliquid_leverage_intent(body)
        action, expires_after = intent["action"], intent["expiresAfter"]
    elif prepared_action is not None:
        if path not in {"/api/order", "/api/grid"}:
            raise hyperliquid_trading.HyperliquidError("Prepared actions are only valid for orders")
        action = prepared_action
    elif path == "/api/order":
        action = hyperliquid_order_action(body)
    elif path == "/api/cancel":
        action = hyperliquid_cancel_action(body)
    else:
        raise hyperliquid_trading.HyperliquidError(READ_ONLY_MESSAGE)
    count = len(action.get("orders") or action.get("cancels") or [1])
    close_target = (str(body["symbol"]).strip().upper(), "buy" if action["orders"][0]["b"] else "sell",
                    action["orders"][0]["s"]) if path == "/api/order" and body.get("closePosition") else None
    if close_target and str(body.get('orderType', '')).strip().lower() == 'mkt':
        close_target = (*close_target, body['position'])
    trader, reason = hyperliquid_trader()
    # Serialize the final permission check with signing/submission and disarming.
    with arm_lock:
        if close_target and str(body.get('orderType', '')).strip().lower() == 'mkt' and body.get('expectedArmed') is not armed:
            raise hyperliquid_trading.HyperliquidError('ARM state changed. Review the close again')
        if path == "/api/leverage":
            if body["expectedArmed"] is not armed:
                raise hyperliquid_trading.HyperliquidError("ARM state changed. Review the leverage change again")
            current = hyperliquid.trading_capacity(str(body["symbol"]).strip().upper())["leverage"]
            if current["value"] != body["expectedLeverage"] or (current["type"] == "cross") != body["cross"]:
                raise hyperliquid_trading.HyperliquidError("Exchange leverage or margin mode changed. Refresh before applying")
        if path == "/api/chart-order" and body["expectedArmed"] is not armed:
            raise hyperliquid_trading.HyperliquidError("ARM state changed. Review the chart change again")
        if path == "/api/grid" and body.get("expectedArmed") is not armed:
            raise hyperliquid_trading.HyperliquidError("ARM state changed. Refresh the grid preview")
        if path == "/api/chart-order" and (not armed or trader is None):
            hyperliquid_chart.validate(intent, hyperliquid)
        if close_target and (not armed or trader is None):
            hyperliquid.validate_close(*close_target)
        if not armed:
            return {"exchange": "hyperliquid", "type": action["type"], "outcome": "simulated", "simulated": True,
                    "live": False, "action": action, "rows": [],
                    "message": "Terminal is DISARMED — the Hyperliquid action was validated but not signed or sent."}
        if trader is None:
            return {"exchange": "hyperliquid", "type": action["type"], "outcome": "simulated", "simulated": True,
                    "live": False, "action": action, "rows": [],
                    "message": f"Not signed: {reason}"}
        if trader.network != hyperliquid.network or trader.account_address != hyperliquid.account_address.lower():
            raise hyperliquid_trading.HyperliquidError("Read and signing account identities disagree")
        hyperliquid.require_agent(trader.address)
        if path == "/api/chart-order":
            hyperliquid_chart.validate(intent, hyperliquid)
        if close_target:
            hyperliquid.validate_close(*close_target)
        result = trader.submit(action, count=count, expires_after=expires_after) if expires_after is not None else trader.submit(action, count=count)
    return {"exchange": "hyperliquid", "type": action["type"], "live": True, **result}


# ---- account helpers -------------------------------------------------------

def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def extract_account(accounts_payload: Any) -> dict[str, Any]:
    if not isinstance(accounts_payload, dict):
        return {}
    accounts = accounts_payload.get("accounts")
    if not isinstance(accounts, dict) or not accounts:
        return {}
    account_id = "flex" if "flex" in accounts else next(iter(accounts))
    account = accounts.get(account_id)
    if not isinstance(account, dict):
        return {}

    balances = account.get("currencies") or account.get("balances") or {}
    currencies = {
        code: info.get("quantity")
        for code, info in balances.items()
        if isinstance(info, dict) and _as_float(info.get("quantity"))
    }
    return {
        "id": account_id,
        "type": account.get("type"),
        "balanceValue": _as_float(account.get("balanceValue")),
        "portfolioValue": _as_float(account.get("portfolioValue")) or _as_float(account.get("marginEquity")),
        "collateralValue": _as_float(account.get("collateralValue")),
        "pnl": _as_float(account.get("pnl")),
        "funding": _as_float(account.get("unrealizedFunding")),
        "availableMargin": _as_float(account.get("availableMargin")),
        "initialMargin": _as_float(account.get("initialMargin")),
        "maintenanceMargin": _as_float(account.get("maintenanceMargin")),
        "totalUnrealized": _as_float(account.get("totalUnrealized")),
        "balances": currencies,
    }


def _unavailable(key: str, error: Exception | str) -> dict[str, Any]:
    previous = cache.peek(key)
    return {
        "error": str(error),
        "state": "unavailable",
        "lastKnown": previous[0] if previous else None,
        "ageSeconds": round(previous[1], 1) if previous else None,
    }


def get_account() -> dict[str, Any]:
    cached = cache.get("account", 3)
    if cached is not None:
        return cached
    try:
        payload = client.get("/accounts", private=True)
        account = extract_account(payload)
        if not account:
            raise KrakenFuturesError("invalid accounts response")
        cache.put("account", account)
        return account
    except KrakenFuturesError as exc:
        return _unavailable("account", exc)


def get_positions() -> list[dict[str, Any]]:
    cached = cache.get("positions", 2)
    if cached is not None:
        return cached
    try:
        payload = client.get("/openpositions", private=True)
        positions = payload.get("openPositions") if isinstance(payload, dict) else None
        if not isinstance(positions, list):
            raise KrakenFuturesError("invalid open positions response")
        result = enrich_positions(positions)
        cache.put("positions", result)
        return result
    except KrakenFuturesError as exc:
        return [_unavailable("positions", exc)]


def enrich_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add the cross-margin liquidation price per position.

    Kraken's REST API does not expose it. Matches Kraken's UI (verified against
    a live position): liquidation when collateral + unrealizedPnl(p) equals the
    account maintenanceMargin at its current value. Split mm pro-rata by
    notional when several positions are open.
    """
    if not positions:
        return positions
    account = get_account()
    acct_mm = _as_float(account.get("maintenanceMargin"))
    collateral = _as_float(account.get("collateralValue")) or _as_float(account.get("balanceValue"))
    if not acct_mm or not collateral:
        return positions

    try:
        instruments = get_instruments().get("instruments", [])
    except KrakenFuturesError:
        instruments = []
    multipliers = {
        str(i.get("symbol")): _as_float(i.get("contractValue")) or 1.0
        for i in instruments
    }

    rows = []
    for p in positions:
        if not isinstance(p, dict) or "error" in p:
            continue
        size = _as_float(p.get("size"))
        entry = _as_float(p.get("price"))
        symbol = str(p.get("symbol"))
        if not size or not entry or size <= 0:
            continue
        mult = multipliers.get(symbol, 1.0)
        ticker = hub.ticker(symbol) or get_ticker_rest(symbol)
        mark = _as_float(ticker.get("markPrice")) if ticker else None
        notional = size * mult * (mark or entry)
        rows.append((p, size, entry, mult, notional))

    total_notional = sum(r[4] for r in rows) or 1.0
    for p, size, entry, mult, notional in rows:
        mm_pos = acct_mm * (notional / total_notional)
        buffer = (collateral - acct_mm) / (size * mult)
        if str(p.get("side")) == "long":
            p_liq = entry - buffer
        else:
            p_liq = entry + buffer
        if p_liq > 0:
            p["liqPriceEstimate"] = round(p_liq, 8)
        try:
            atr = binance_candles.atr14d(str(p.get("symbol")))
            if atr:
                p["atr14d"] = atr[0]  # daily ATR(14) in price units (Binance 1d)
        except Exception:
            pass
    return positions


def get_orders() -> list[dict[str, Any]]:
    cached = cache.get("orders", 2)
    if cached is not None:
        return cached
    try:
        payload = client.get("/openorders", private=True)
        orders = payload.get("openOrders") if isinstance(payload, dict) else None
        if not isinstance(orders, list):
            raise KrakenFuturesError("invalid open orders response")
        cache.put("orders", orders)
        return orders
    except KrakenFuturesError as exc:
        return [_unavailable("orders", exc)]


def get_fills() -> list[dict[str, Any]]:
    cached = cache.get("fills", 10)
    if cached is not None:
        return cached
    try:
        payload = client.get("/fills", private=True)
        fills = payload.get("fills", []) if isinstance(payload, dict) else []
        result = fills if isinstance(fills, list) else []
        cache.put("fills", result)
        return result
    except KrakenFuturesError as exc:
        return [{"error": str(exc)}]


def get_instruments() -> dict[str, Any]:
    cached = cache.get("instruments", 600)
    if cached is not None:
        return cached
    payload = client.get("/instruments")
    result = {"instruments": payload.get("instruments", [])} if isinstance(payload, dict) else {"instruments": []}
    cache.put("instruments", result)
    return result


def get_ticker_rest(symbol: str) -> dict[str, Any] | None:
    cached = cache.get(f"ticker:{symbol}", 2)
    if cached is not None:
        return cached or None
    try:
        payload = client.get("/tickers")
        match = next(
            (t for t in payload.get("tickers", []) if t.get("symbol") == symbol),
            None,
        ) if isinstance(payload, dict) else None
        if match:
            match = {**match, "_receivedAt": time.time()}
            cache.put(f"ticker:{symbol}", match)
        return match
    except KrakenFuturesError:
        return None


def _refresh_after_action(action: dict[str, Any], result: dict[str, Any], is_armed: bool) -> None:
    """Invalidate REST state after every action; wait for closes before dependent actions."""
    cache.drop("positions", "orders", "account")
    if not is_armed or action.get("type") != "close" or result.get("ok") is not True:
        return
    symbol = str(action.get("symbol") or "")
    expected = float(result.get("remainingSize") or 0)
    deadline = time.monotonic() + 3.0
    last_error = None
    while time.monotonic() < deadline:
        cache.drop("positions")
        positions = get_positions()
        failed = next((p.get("error") for p in positions if p.get("error")), None)
        if failed:
            last_error = failed
        else:
            position = next((p for p in positions if p.get("symbol") == symbol), None)
            current = float(position.get("size") or 0) if position else 0.0
            if current <= expected + max(1e-9, expected * 1e-8):
                return
        time.sleep(0.1)
    detail = f" ({last_error})" if last_error else ""
    raise trading_actions.ActionError(f"position state did not refresh after closing {symbol}{detail}; remaining batch skipped")


# ---- candles ---------------------------------------------------------------

action_ctx = ActionContext(
    client=client,
    hub=hub,
    get_account=get_account,
    get_positions=get_positions,
    get_orders=get_orders,
    get_instruments=get_instruments,
    get_ticker_rest=get_ticker_rest,
)
db = db_mod.Database()


def _ticker_payload(symbol: str) -> dict[str, Any]:
    try:
        ticker = action_ctx.fresh_ticker(symbol)
        return {**ticker, "state": "current", "ageSeconds": round(action_ctx._ticker_age(ticker) or 0, 1)}
    except trading_actions.ActionError as exc:
        ticker = hub.ticker(symbol)
        if not ticker:
            previous = cache.peek(f"ticker:{symbol}")
            ticker = previous[0] if previous and isinstance(previous[0], dict) else {}
        age = action_ctx._ticker_age(ticker) if ticker else None
        return {
            **ticker,
            "state": "unavailable",
            "error": str(exc),
            "ageSeconds": round(age, 1) if age is not None else None,
        }


def _publish_chase(kind: str, payload: dict[str, Any]) -> None:
    db.log_event("chase", payload)
    sse.publish(kind, payload)


def _set_protection_alert(symbol: str, kind: str, details: dict[str, Any]) -> None:
    payload = {"symbol": symbol, "kind": kind, "status": "UNPROTECTED", "details": details}
    db.set_protection_alert(symbol, kind, details)
    db.log_event("protection_alert", payload)
    sse.publish("protection_alert", payload)


def _clear_protection_alert(symbol: str, kind: str) -> None:
    db.clear_protection_alert(symbol, kind)
    payload = {"symbol": symbol, "kind": kind, "status": "RESTORED"}
    db.log_event("protection_alert", payload)
    sse.publish("protection_alert", payload)


chase_manager = chase_mod.ChaseManager(_publish_chase)
binance_klines = binance_ws_mod.BinanceKlineStream(hub, sse.publish)
binance_klines.start()


def _equity_snapshot_loop() -> None:
    # Record portfolio value every 30s so the equity curve includes open PnL.
    while True:
        try:
            acct = get_account()
            if isinstance(acct, dict) and acct.get("balanceValue") is not None:
                bal = float(acct.get("balanceValue") or 0)
                unrl = float(acct.get("totalUnrealized") or 0)
                eq = float(acct.get("portfolioValue") or (bal + unrl))
                db.upsert_equity(int(time.time()), bal, unrl, eq)
        except Exception:
            pass
        time.sleep(30)


def _detect_orphan_chases() -> None:
    try:
        orders = get_orders()
        failed = next((order.get("error") for order in orders if order.get("error")), None)
        if failed:
            db.log_event("chase_orphan_scan", {"ok": False, "error": str(failed)[:300]})
            return
        found = chase_manager.recover(db.latest_chase_snapshots(), orders, action_ctx)
        db.log_event("chase_orphan_scan", {"ok": True, "count": len(found)})
    except Exception as exc:
        db.log_event("chase_orphan_scan", {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]})


threading.Thread(target=_equity_snapshot_loop, name="equity-snapshots", daemon=True).start()
threading.Thread(target=_detect_orphan_chases, name="chase-orphan-scan", daemon=True).start()
action_ctx.chase = chase_manager


def _start_chase_if_armed(spec: dict[str, Any]) -> dict[str, Any]:
    with arm_lock:
        if not armed:
            raise trading_actions.ActionError("chase requires an ARMED terminal")
        symbol = str(spec["symbol"])
        if spec.get("reduceOnly"):
            action_ctx.fresh_ticker(symbol)
        else:
            action_ctx.require_new_exposure(symbol)
        return chase_manager.start(spec, action_ctx)


action_ctx.start_chase = _start_chase_if_armed
action_ctx.refresh_orders = lambda: cache.drop("orders")
action_ctx.set_protection_alert = _set_protection_alert
action_ctx.clear_protection_alert = _clear_protection_alert
action_ctx.after_action = _refresh_after_action
_legacy_managed_protection_ids = db.inferred_managed_protection_ids()
_protection_sync_due: dict[tuple[Any, ...], float] = {}


def _reconcile_protection_alerts(positions: list[dict[str, Any]], orders: list[dict[str, Any]]) -> None:
    if any(row.get("error") for row in positions + orders if isinstance(row, dict)):
        return
    by_symbol = {str(position.get("symbol")): position for position in positions if position.get("symbol")}
    for alert in db.protection_alerts():
        symbol, kind = alert["symbol"], alert["kind"]
        position = by_symbol.get(symbol)
        if not position or not (_as_float(position.get("size")) or 0) > 0:
            _clear_protection_alert(symbol, kind)
            continue
        if trading_actions.protection_covers(orders, symbol, kind, position.get("size")):
            _clear_protection_alert(symbol, kind)


def _managed_protection_loop() -> None:
    """Keep app-managed full-position TP/SL orders aligned with live position size."""
    while True:
        time.sleep(2.5)
        with arm_lock:
            is_armed = armed
        try:
            positions, orders = get_positions(), get_orders()
            _reconcile_protection_alerts(positions, orders)
            if not is_armed:
                _protection_sync_due.clear()
                continue
            candidates = trading_actions.managed_protection_sync_actions(
                positions, orders, _legacy_managed_protection_ids,
            )
            now = time.monotonic()
            active_keys = {
                (a["type"], a["symbol"], a["stopPrice"], a["syncFromSize"], a["syncToSize"], a["sourceCliOrdId"])
                for a in candidates
            }
            for key in list(_protection_sync_due):
                if key not in active_keys:
                    del _protection_sync_due[key]
            for action in candidates:
                key = (action["type"], action["symbol"], action["stopPrice"], action["syncFromSize"], action["syncToSize"], action["sourceCliOrdId"])
                due = _protection_sync_due.get(key)
                if due is None:
                    _protection_sync_due[key] = now + 2.5  # require two stable observations
                    continue
                if now < due:
                    continue
                with arm_lock:
                    if not armed:
                        break
                    results = trading_actions.execute_actions([action], action_ctx, True)
                db.log_action("protection_sync", True, [action], results)
                result = results[0] if results else {"ok": False, "error": "no result"}
                payload = {
                    "symbol": action["symbol"],
                    "kind": "TP" if action["type"] == "replace_tp" else "SL",
                    "fromSize": action["syncFromSize"],
                    "toSize": action["syncToSize"],
                    "ok": result.get("ok") is True,
                    "error": result.get("error"),
                }
                db.log_event("protection_sync", payload)
                sse.publish("protection_sync", payload)
                _protection_sync_due[key] = now + (30 if result.get("ok") is not True else 5)
                break  # one live replacement per cycle
        except Exception as exc:
            sys.stderr.write(f"[protection-sync] {type(exc).__name__}: {exc}\n")


tp_cleanup = TakeProfitCleanup(db, action_ctx, chase_manager,
                               lambda: cache.drop("positions", "orders"), sse.publish)


def _tp_cleanup_loop() -> None:
    while True:
        time.sleep(2.5)
        try:
            with arm_lock:
                tp_cleanup.step(armed)
        except Exception as exc:
            sys.stderr.write(f"[tp-cleanup] {type(exc).__name__}: {exc}\n")


threading.Thread(target=_tp_cleanup_loop, name="tp-cleanup", daemon=True).start()
threading.Thread(target=_managed_protection_loop, name="managed-protection", daemon=True).start()

RESOLUTIONS = {"1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"}


def _merge_hub_volumes(symbol: str, resolution: str, candles: list[list[Any]]) -> None:
    """Kraken's public charts API reports volume 0; overlay real traded volume
    accumulated by the market hub from the live trade feed (covers the window
    since server start)."""
    hub_candles = hub.candles_1m(symbol, limit=1500)
    if not hub_candles:
        return
    res_seconds = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
                   "4h": 14400, "12h": 43200, "1d": 86400, "1w": 604800}[resolution]
    by_bucket: dict[int, float] = {}
    for t, _o, _h, _l, _c, v in hub_candles:
        bucket = int(t // res_seconds) * res_seconds
        by_bucket[bucket] = by_bucket.get(bucket, 0.0) + (v or 0.0)
    for c in candles:
        vol = by_bucket.get(c[0])
        if vol is not None:
            c[5] = vol


def get_candles(symbol: str, resolution: str) -> dict[str, Any]:
    if resolution not in RESOLUTIONS:
        return {"error": f"unsupported resolution {resolution!r}", "candles": []}

    # primary: Binance USDT-M klines (deep history, real volume); Kraken fallback below
    binance = binance_candles.get_klines(symbol, resolution)
    if binance:
        return {"symbol": symbol, "resolution": resolution, "source": "binance", "candles": binance}

    try:
        payload = client.get_public_charts(symbol, resolution)
        raw = payload.get("candles", []) if isinstance(payload, dict) else []
        candles = []
        for c in raw:
            if isinstance(c, dict):
                t = c.get("time")
                t = float(t) if not isinstance(t, (int, float)) else float(t)
                if t > 1e12:  # Kraken charts API returns milliseconds
                    t = t / 1000.0
                candles.append([
                    int(t), _as_float(c.get("open")), _as_float(c.get("high")),
                    _as_float(c.get("low")), _as_float(c.get("close")), _as_float(c.get("volume")),
                ])
            elif isinstance(c, (list, tuple)) and len(c) >= 6:
                candles.append([int(c[0])] + [float(x) for x in c[1:6]])
        if candles:
            _merge_hub_volumes(symbol, resolution, candles)
            return {"symbol": symbol, "resolution": resolution, "source": "rest", "candles": candles}
    except KrakenFuturesError:
        pass

    if resolution == "1m":
        hub_candles = hub.candles_1m(symbol)
        if hub_candles:
            return {"symbol": symbol, "resolution": resolution, "source": "hub", "candles": hub_candles}

    return {"symbol": symbol, "resolution": resolution, "source": "none", "candles": []}


# ---- order entry -----------------------------------------------------------

ALLOWED_ORDER_TYPES = {"mkt", "lmt", "post", "ioc", "stp", "take_profit"}
LIMIT_TYPES = {"lmt", "post", "ioc"}
TRIGGER_TYPES = {"stp", "take_profit"}


def validate_order_params(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    symbol = str(body.get("symbol") or "").strip().upper()
    side = str(body.get("side") or "").strip().lower()
    order_type = str(body.get("orderType") or "").strip().lower()
    size = _as_float(body.get("size"))

    if not symbol.startswith("PF_"):
        return None, "PF_ symbol is required"
    if side not in {"buy", "sell"}:
        return None, "side must be buy or sell"
    if order_type not in ALLOWED_ORDER_TYPES:
        return None, f"orderType must be one of {sorted(ALLOWED_ORDER_TYPES)}"
    if size is None or size <= 0:
        return None, "size must be a positive number"

    params: dict[str, Any] = {"symbol": symbol, "side": side, "orderType": order_type, "size": size}
    if order_type in LIMIT_TYPES:
        limit_price = _as_float(body.get("limitPrice"))
        if limit_price is None or limit_price <= 0:
            return None, "limitPrice is required for limit order types"
        params["limitPrice"] = limit_price
    if order_type in TRIGGER_TYPES:
        stop_price = _as_float(body.get("stopPrice"))
        if stop_price is None or stop_price <= 0:
            return None, "stopPrice is required for trigger order types"
        params["stopPrice"] = stop_price
        params["triggerSignal"] = str(body.get("triggerSignal") or "mark")
    if body.get("reduceOnly"):
        params["reduceOnly"] = True
    try:
        instrument = next(
            (i for i in get_instruments().get("instruments", []) if i.get("symbol") == symbol),
            {},
        )
        params = trading_actions.apply_size_precision(params, instrument)
    except trading_actions.ActionError as exc:
        return None, str(exc)
    return params, None


def place_order(params: dict[str, Any]) -> dict[str, Any]:
    with arm_lock:
        if not armed:
            return {
                "outcome": "simulated", "simulated": True,
                "message": "Terminal is DISARMED — order was not sent. Arm the terminal to trade live.",
                "params": params, "order": params,
            }
        if not params.get("reduceOnly"):
            action_ctx.require_new_exposure(params["symbol"])
        submitted = trading_actions.submit_one(action_ctx, params, f"kt-ticket-{params['symbol']}")
    cache.drop("positions", "orders", "account", "fills")
    result = {"simulated": False, "order": submitted["params"], **submitted}
    if submitted["outcome"] != "confirmed":
        result["error"] = submitted.get("error") or f"order outcome {submitted['outcome']}"
    return result


def cancel_order(body: dict[str, Any]) -> dict[str, Any]:
    cli_ord_id = body.get("cliOrdId")
    order_id = body.get("orderId") or body.get("order_id")
    with arm_lock:
        if not armed:
            return {"outcome": "simulated", "simulated": True, "message": "DISARMED — cancel not sent.", "target": cli_ord_id or order_id}
        if cli_ord_id:
            target = {"cliOrdId": str(cli_ord_id)}
        elif order_id:
            target = {"order_id": str(order_id)}
        else:
            return {"outcome": "rejected", "error": "cliOrdId or orderId required"}
        result = trading_actions.cancel_one(action_ctx, target)
    cache.drop("positions", "orders", "account")
    return {"simulated": False, **result, **({"error": result.get("error") or f"cancel outcome {result['outcome']}"}
                                                    if result["outcome"] != "confirmed" else {})}


def _flatten_outcome(results: list[dict[str, Any]], is_armed: bool) -> str:
    if not results:
        return "confirmed" if is_armed else "simulated"
    outcomes = [str(result.get("outcome") or "unknown") for result in results]
    if not is_armed and all(outcome == "simulated" for outcome in outcomes):
        return "simulated"
    if all(outcome == "confirmed" for outcome in outcomes):
        return "confirmed"
    if any(outcome == "confirmed" for outcome in outcomes):
        return "partial"
    if any(outcome == "unknown" for outcome in outcomes):
        return "unknown"
    return "rejected"


def flatten_all(mode: str, symbol: str = "") -> dict[str, Any]:
    """Run a server-authoritative flatten plan under the global ARM/write lock."""
    with arm_lock:
        is_armed = armed
        if is_armed:
            cache.drop("positions", "orders", "account")
        try:
            actions = trading_actions.build_flatten_actions(
                mode,
                get_positions(),
                get_orders() if mode == "emergency" and not is_armed else None,
                symbol=symbol,
                defer_order_validation=is_armed and mode == "emergency",
            )
        except trading_actions.ActionError as exc:
            return {"mode": mode, "armed": is_armed, "outcome": "rejected", "error": str(exc), "actions": [], "results": []}
        except Exception as exc:
            return {"mode": mode, "armed": is_armed, "outcome": "unknown",
                    "error": f"flatten preflight failed: {type(exc).__name__}: {exc}", "actions": [], "results": []}

        aborting = {"requested": [], "completed": [], "pending": []}
        if is_armed and mode == "chase" and actions:
            try:
                active_chases = chase_manager.active()
                if symbol:
                    active_chases = [item for item in active_chases
                                     if (item.get("symbol") or item.get("spec", {}).get("symbol")) in {None, "", symbol}]
            except Exception as exc:
                return {"mode": mode, "armed": True, "outcome": "unknown",
                        "error": f"could not inspect active Chase workers: {type(exc).__name__}: {exc}",
                        "actions": actions, "results": []}
            if active_chases:
                unresolved = any(item.get("status") in {"unknown", "orphaned"} for item in active_chases)
                return {
                    "mode": mode, "armed": True, "outcome": "unknown" if unresolved else "rejected",
                    "error": "existing Chase workers/orders must finish or be resolved before soft flatten",
                    "actions": actions, "results": [], "activeChases": active_chases,
                    "abortingChases": aborting,
                }

        if is_armed and mode == "emergency":
            def stop_chases() -> dict[str, Any]:
                stopped = chase_manager.abort_all()
                cache.drop("orders", "account")
                return stopped

            results, aborting = trading_actions.execute_emergency_flatten(actions, action_ctx, stop_chases)
        else:
            results = trading_actions.execute_actions(actions, action_ctx, is_armed, max_actions=None) if actions else []

        outcome = _flatten_outcome(results, is_armed)
        error = next((str(result.get("stateRefreshError") or result.get("error")) for result in results
                      if result.get("stateRefreshError") or result.get("outcome") not in {"confirmed", "simulated"}), None)
        final_state = None
        if is_armed and mode == "emergency" and outcome == "confirmed":
            cache.drop("positions", "orders", "account")
            try:
                final_positions = get_positions()
                final_orders = get_orders()
                final_plan = trading_actions.build_flatten_actions("emergency", final_positions, final_orders)
                remaining_positions = sum(action["type"] == "close" for action in final_plan)
                final_state = {"positionCount": remaining_positions, "orderCount": len(final_orders)}
                if remaining_positions or final_orders:
                    outcome = "partial"
                    error = (f"final verification found {remaining_positions} open position(s) "
                             f"and {len(final_orders)} open order(s); run emergency flatten again")
            except Exception as exc:
                outcome = "unknown"
                error = f"final flat-state verification failed: {type(exc).__name__}: {exc}"
    cache.drop("positions", "orders", "account", "fills")
    return {
        "mode": mode,
        "armed": is_armed,
        "simulated": outcome == "simulated",
        "outcome": outcome,
        "actions": actions,
        "results": results,
        "positionCount": sum(action["type"] in {"close", "chase"} for action in actions),
        "closedPositionCount": sum(
            result.get("type") == "close" and result.get("outcome") == "confirmed" and not result.get("noOp")
            for result in results
        ),
        "startedChaseCount": sum(bool(result.get("chase")) for result in results),
        "cancelledOrderCount": sum(
            nested.get("outcome") == "confirmed"
            for result in results if result.get("type") == "cancel_all_orders"
            for nested in result.get("results", [])
        ),
        "abortingChases": aborting,
        **({"finalState": final_state} if final_state is not None else {}),
        **({"noOp": True} if not actions or (results and all(result.get("noOp") for result in results)) else {}),
        **({"error": error or f"flatten outcome {outcome}"} if outcome not in {"confirmed", "simulated"} else {}),
    }


# ---- HTTP handler ----------------------------------------------------------

class TerminalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KrakenFuturesTerminal/0.1"

    # ---- plumbing ----

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send_json(self, obj: Any, status: int = 200) -> None:
        request_id = getattr(self, "_write_request_id", None)
        if request_id:
            db.complete_write_request(request_id, status, obj)
            self._write_request_id = None
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > 1_000_000:
            return None
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        elif path == "/volatility":
            path = "/volatility.html"
        target = safe_static_path(STATIC_DIR, path)
        if target is None:
            self._send_json({"error": "not found"}, 404)
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".json": "application/json",
        }
        body = target.read_bytes()
        if target.suffix == ".html":
            body = body.replace(b"__TERMINAL_TOKEN__", security.token.encode("ascii"))
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- GET ----

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if not path.startswith("/api/"):
            self._serve_static(path)
            return

        try:
            exchange = requested_exchange(parsed.query)
            if path not in {"/api/session", "/api/exchanges", "/api/arm/challenge"}:
                exchange_routing.check(exchange)
                if exchange == "hyperliquid":
                    if path == "/api/stream":
                        self._stream_sse(exchange)
                    elif path == "/api/order-lifecycle":
                        try:
                            request_id = query.get("requestId", "")
                            if not REQUEST_ID_RE.fullmatch(request_id):
                                raise hyperliquid_trading.HyperliquidError("valid requestId required")
                            self._send_json(hyperliquid_lifecycle.inspect_order(db, hyperliquid, request_id))
                        except (hyperliquid_trading.HyperliquidError, ValueError) as exc:
                            self._send_json({"state": "unavailable", "error": str(exc)}, 503)
                    elif path == "/api/fill-history":
                        try:
                            self._send_json(hyperliquid_fills.order_totals(db, hyperliquid, query.get("orderId")))
                        except (hyperliquid_trading.HyperliquidError, ValueError) as exc:
                            self._send_json({"state": "unavailable", "error": str(exc)}, 503)
                    elif path == "/api/cancel-recovery":
                        try:
                            self._send_json(hyperliquid_recovery.cancellations(db, hyperliquid, query.get("requestId")))
                        except (hyperliquid_trading.HyperliquidError, ValueError) as exc:
                            self._send_json({"state": "unavailable", "error": str(exc)}, 503)
                    elif path == "/api/execution-recovery":
                        try:
                            self._send_json(hyperliquid_recovery.unresolved(db, hyperliquid))
                        except (hyperliquid_trading.HyperliquidError, ValueError) as exc:
                            self._send_json({"state": "unavailable", "error": str(exc)}, 503)
                    elif path == "/api/health":
                        with arm_lock:
                            exchange_routing.check(exchange)
                            # ARM is process-wide, so report the real flag, never a constant.
                            self._send_json({**hyperliquid.health(), **exchange_routing.snapshot(),
                                             "armed": armed, "readOnly": False,
                                             "accountAddress": hyperliquid.account_address,
                                             "signedTrading": hyperliquid_gate()})
                    elif path == "/api/volatility":
                        try:
                            self._send_json(scanner.scan_volatility_hyperliquid(
                                hyperliquid,
                                window_minutes=int(query.get("window", "5")),
                                limit=int(query.get("limit", "15")),
                                min_volume_quote=float(query.get("minVolume", "1000000")),
                                max_spread_percent=float(query.get("maxSpread", "0.5")),
                            ))
                        except (hyperliquid_trading.HyperliquidError, ValueError) as exc:
                            self._send_json({"error": str(exc), "rows": []}, 503)
                    else:
                        payload, status = hyperliquid.read(path, query)
                        self._send_json(payload, status)
                    return
            if path == "/api/exchanges":
                # readOnly means "no browser trading UI for this venue"; signedTrading is the server gate.
                self._send_json({**exchange_routing.snapshot(), "venues": [
                    {"id": "kraken", "name": "Kraken Futures", "readOnly": False},
                    {"id": "hyperliquid", "name": "Hyperliquid", "readOnly": True,
                     "signedTrading": hyperliquid_gate()["mode"]},
                ]})
            elif path == "/api/session":
                if not security.valid_host(self.headers):
                    self._send_json({"error": "invalid Host"}, 403)
                    return
                self._send_json({"token": security.token, **exchange_routing.snapshot()})
            elif path == "/api/arm/challenge":
                if not security.valid_token(self.headers):
                    self._send_json({"error": "invalid terminal token"}, 403)
                    return
                self._send_json({"challenge": security.issue_arm_challenge()})
            elif path == "/api/health":
                with arm_lock:
                    exchange_routing.check(exchange)
                    self._send_json({
                        "ok": True,
                        **exchange_routing.snapshot(),
                        "readOnly": False,
                        "armed": armed,
                        "env": "demo" if client.is_demo else "live",
                        "hub": hub.status(),
                        "binanceKlines": binance_klines.status(),
                        "hasKeys": bool(client.api_key and client.api_secret),
                        "aiModel": os.getenv("AI_CHAT_MODEL", ai_chat.DEFAULT_MODEL),
                    })
            elif path == "/api/alt-btc":
                window = query.get("window", "24h")
                if window not in alt_btc.WINDOWS:
                    self._send_json({"error": "Window must be 1h, 6h, or 24h"}, 400)
                    return
                try:
                    self._send_json(alt_btc.get_snapshot(get_instruments().get("instruments"), window=window))
                except Exception as exc:
                    self._send_json({"state": "unavailable", "error": f"Alt/BTC data unavailable: {exc}"}, 503)
            elif path == "/api/instruments":
                self._send_json(get_instruments())
            elif path == "/api/tickers":
                symbols = query.get("symbols", "")
                if symbols:
                    hub.watch(symbols.split(","))
                self._send_json({"tickers": hub.tickers(), "watchlist": hub.watchlist()})
            elif path == "/api/candles":
                symbol = query.get("symbol", "").upper()
                resolution = query.get("res", "1m")
                if not symbol:
                    self._send_json({"error": "symbol required"}, 400)
                    return
                hub.watch([symbol])
                self._send_json(get_candles(symbol, resolution))
            elif path == "/api/orderbook":
                symbol = query.get("symbol", "").upper()
                if not symbol:
                    self._send_json({"error": "symbol required"}, 400)
                    return
                self._send_json(client.get("/orderbook", params={"symbol": symbol}))
            elif path == "/api/trades":
                symbol = query.get("symbol", "").upper()
                limit = int(query.get("limit", "50"))
                self._send_json({"trades": hub.recent_trades(symbol, limit)})
            elif path == "/api/account":
                self._send_json(_account_payload(get_account()))
            elif path == "/api/positions":
                self._send_json(_rows_payload("positions", get_positions()))
            elif path == "/api/orders":
                self._send_json(_rows_payload("orders", get_orders()))
            elif path == "/api/equity":
                self._send_json({"rows": db.get_equity()})
            elif path == "/api/stats":
                try:
                    out = account_log.compact_rows(client)
                except Exception as exc:
                    self._send_json({"error": str(exc), "rows": []})
                    return
                self._send_json({"rows": out})
            elif path == "/api/fills":
                self._send_json({"fills": get_fills()})
            elif path == "/api/marketlist":
                self._send_json({"rows": _marketlist()})
            elif path == "/api/volatility":
                self._send_json(scanner.scan_volatility(
                    client,
                    window_minutes=int(query.get("window", "5")),
                    limit=int(query.get("limit", "15")),
                    min_volume_quote=float(query.get("minVolume", "1000000")),
                    max_spread_percent=float(query.get("maxSpread", "0.5")),
                ))
            elif path == "/api/signal":
                symbol = query.get("symbol", "").upper()
                if not symbol:
                    self._send_json({"error": "symbol required"}, 400)
                    return
                try:
                    self._send_json(scanner.ema_signal(client, symbol, int(query.get("fast", "400")), int(query.get("slow", "800"))))
                except (KrakenFuturesError, ValueError) as exc:
                    self._send_json({"symbol": symbol, "side": None, "error": str(exc)})
            elif path == "/api/chat/history":
                session = db.ensure_session()
                msgs = db.get_messages(session["id"], limit=80)
                # Older executions predate atomic proposal claims. Hide the nearest
                # proposal for each persisted execution trace so stale cards cannot reappear.
                for index, msg in enumerate(msgs):
                    if not ((msg.get("meta") or {}).get("trace")):
                        continue
                    for previous in reversed(msgs[:index]):
                        blocks = (previous.get("meta") or {}).get("actionProposals") or []
                        if blocks:
                            blocks.pop(0)
                            break
                self._send_json({"session": {"id": session["id"], "title": session["title"], "summary": bool(session["summary"])}, "messages": msgs})
            elif path == "/api/chase":
                self._send_json({"chases": chase_manager.list()})
            elif path == "/api/tp-cleanup":
                self._send_json({"cleanups": db.tp_cleanup_states()})
            elif path == "/api/protection/alerts":
                self._send_json({"alerts": db.protection_alerts()})
            elif path == "/api/debug/threads" and DEBUG_ENDPOINTS:
                import sys as _sys
                import threading as _threading
                import traceback as _tb
                parts = []
                names = {t.ident: t.name for t in _threading.enumerate()}
                for tid, frame in _sys._current_frames().items():
                    name = names.get(tid, str(tid))
                    stack = "".join(_tb.format_stack(frame))
                    parts.append(f"=== {name} ===\n{stack}")
                self._send_json({"dump": "\n".join(parts)})
            elif path == "/api/stream":
                self._stream_sse()
            else:
                self._send_json({"error": "unknown endpoint"}, 404)
        except ExchangeRoutingError as exc:
            self._send_json({"error": str(exc), **exchange_routing.snapshot()}, exc.status)
        except KrakenHTTPError as exc:
            self._send_json({"error": str(exc), "status": exc.status, "payload": exc.payload}, 502)
        except KrakenFuturesError as exc:
            self._send_json({"error": str(exc)}, 502)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_sse(self, exchange="kraken") -> None:
        bus = sse if exchange == "kraken" else hyperliquid_sse
        stream_epoch = exchange_routing.snapshot()["exchangeEpoch"]
        q = bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            status = hub.status() if exchange == "kraken" else hyperliquid.health()["hub"]
            watchlist = hub.watchlist() if exchange == "kraken" else hyperliquid.watchlist()
            hello = json.dumps({"status": status, "watchlist": watchlist, **exchange_routing.snapshot()})
            self.wfile.write(f"event: status\ndata: {hello}\n\n".encode("utf-8"))
            # Reconnecting tabs must receive resolved Chase states missed during restart.
            for chase in chase_manager.list() if exchange == "kraken" else []:
                self.wfile.write(f"event: chase\ndata: {json.dumps(chase, default=str)}\n\n".encode("utf-8"))
            self.wfile.flush()
            idle = 0.0
            while True:
                selected = exchange_routing.snapshot()
                if selected["exchange"] != exchange or selected["exchangeEpoch"] != stream_epoch:
                    self.wfile.write(f"event: exchange\ndata: {json.dumps(selected)}\n\n".encode())
                    self.wfile.flush()
                    return
                try:
                    kind, payload = q.get(timeout=1.0)
                    data = json.dumps(payload, default=str)
                    self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    idle = 0.0
                except queue.Empty:
                    idle += 1.0
                    # comment line keeps intermediaries from closing the stream
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    if idle >= 60 * 5:
                        return
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bus.unsubscribe(q)

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        self._write_request_id = None
        denied = security.validate_write(self.headers)
        if denied:
            status, message = denied
            self.close_connection = True
            self._send_json({"error": message}, status)
            return
        body = self._read_body()
        if body is None:
            self.close_connection = True
            self._send_json({"error": "request body must be a JSON object"}, 400)
            return
        try:
            exchange = requested_exchange(parsed.query)
            epoch = self.headers.get("X-Terminal-Exchange-Epoch")
            if path == "/api/exchange":
                def disarm():
                    global armed
                    armed = False
                with arm_lock:
                    selected = exchange_routing.switch(exchange, epoch, body.get("exchange"),
                                                        active_chases=chase_manager.active, disarm=disarm)
                    if selected["exchange"] != exchange:
                        sse.publish("armed", {"armed": armed})
                        sse.publish("exchange", selected)
                        hyperliquid_sse.publish("exchange", selected)
                        db.log_event("exchange_switch", {"from": exchange, **selected, "armed": armed})
                    self._send_json({**selected, "armed": armed})
                return
            if "exchange" in body and body["exchange"] != exchange:
                raise ExchangeRoutingError("Exchange target disagrees with the request body", 400)
            # Keep the lease across the whole request, including AI work. A switch
            # rejects in-flight requests rather than racing their later writes.
            with exchange_routing.request(exchange, epoch):
                if exchange == "hyperliquid" and path not in HL_WRITE_PATHS | VENUE_NEUTRAL_PATHS:
                    # Unsupported venue actions never reach the write journal or Kraken.
                    self._send_json({"outcome": "rejected", "error": READ_ONLY_MESSAGE,
                                     "exchange": "hyperliquid"}, 405)
                    return
                if not self._claim_write(path, body):
                    return
                if exchange == "hyperliquid" and path not in VENUE_NEUTRAL_PATHS:
                    self._hyperliquid_post(path, body)
                else:
                    self._do_kraken_post(path, body)
        except ExchangeRoutingError as exc:
            self._send_json({"error": str(exc), **exchange_routing.snapshot()}, exc.status)

    def _claim_write(self, path: str, body: dict[str, Any]) -> bool:
        """Persisted request identity, applied to every exchange's intentional writes."""
        if path not in IDEMPOTENT_WRITE_PATHS:
            return True
        request_id = str(body.get("requestId") or "")
        if not REQUEST_ID_RE.fullmatch(request_id):
            self._send_json({"error": "valid requestId required"}, 400)
            return False
        payload = {key: value for key, value in body.items() if key != "requestId"}
        if requested_exchange(urlparse(self.path).query) == "hyperliquid":
            payload = {"venue": "hyperliquid", "network": hyperliquid.network,
                       "account": hyperliquid.account_address.lower(), "body": payload}
        claim = db.claim_write_request(request_id, path, payload)
        if claim["state"] == "blocked":
            self._send_json({"outcome": "rejected", "error": "Resolve the prior Hyperliquid submission before placing another order",
                             "unresolvedRequestId": claim["requestId"]}, 409)
            return False
        if claim["state"] == "conflict":
            self._send_json({"error": "requestId was already used for different input"}, 409)
            return False
        if claim["state"] == "pending":
            self._send_json({
                "outcome": "unknown",
                "error": "request is already in progress or ended before its result was stored; not resubmitted",
            }, 409)
            return False
        if claim["state"] == "replay":
            self._send_json(claim["result"], claim["status"])
            return False
        self._write_request_id = request_id
        return True

    def _hyperliquid_post(self, path: str, body: dict[str, Any]) -> None:
        try:
            if path == "/api/grid/preview":
                try:
                    result = hyperliquid_grid.preview(body, hyperliquid)
                except ValueError as exc:
                    raise hyperliquid_trading.HyperliquidError(str(exc)) from exc
                with arm_lock:
                    result["armed"] = armed
                self._send_json(result)
                return
            if path == "/api/fill-history/sync":
                self._send_json(hyperliquid_fills.sync(db, hyperliquid, body.get("startTime"), body.get("endTime")))
                return
            if path == "/api/cancel-reconcile":
                request_id = body.get("requestId")
                if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
                    raise hyperliquid_trading.HyperliquidError("valid requestId required")
                self._send_json(hyperliquid_recovery.reconcile_cancel(db, hyperliquid, request_id, body.get("target")))
                return
            if path == "/api/order-reconcile":
                request_id = body.get("requestId")
                if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
                    raise hyperliquid_trading.HyperliquidError("valid requestId required")
                self._send_json(hyperliquid_recovery.reconcile(db, hyperliquid, request_id))
                return
            prepared = None
            if path in {"/api/order", "/api/leverage", "/api/chart-order", "/api/grid"}:
                if path == "/api/grid":
                    # GridError is a ValueError, so it has to be mapped before the
                    # journal's own ValueError handling swallows the reason.
                    try:
                        prepared = hyperliquid_grid.prepare(body, hyperliquid, body.get("cloids"))
                    except trading_actions.GridError as exc:
                        raise hyperliquid_trading.HyperliquidError(str(exc)) from exc
                else:
                    prepared = (hyperliquid_chart.prepare(body, hyperliquid) if path == "/api/chart-order" else
                                hyperliquid_order_action(body) if path == "/api/order" else hyperliquid_leverage_intent(body))
                try:
                    db.prepare_hyperliquid_order(self._write_request_id, prepared)
                except ValueError as exc:
                    raise hyperliquid_trading.HyperliquidError(str(exc)) from exc
            result = hyperliquid_write(path, body, prepared_action=prepared)
        except hyperliquid_trading.HyperliquidError as exc:
            self._send_json({"outcome": "rejected", "error": str(exc), "exchange": "hyperliquid"}, 400)
            return
        if result["type"] == "cancel":
            targets = result["action"]["cancels"]
            rows = result.get("rows") or []
            mapped = len(rows) == len(targets) and result.get("outcome") in {"confirmed", "partial", "rejected"}
            result["cancelResults"] = []
            for index, target in enumerate(targets):
                row = rows[index] if mapped else {}
                outcome = ("simulated" if result.get("simulated") else
                           "confirmed" if row.get("state") == "ok" else
                           "rejected" if row.get("state") == "error" or result.get("outcome") == "rejected" else "unknown")
                result["cancelResults"].append({"orderId": str(target["o"]), "asset": target["a"],
                                                "outcome": outcome,
                                                "error": None if outcome in {"confirmed", "simulated"} else row.get("error") or result.get("error")})
        if path == "/api/grid":
            orders = result["action"].get("orders") or []
            result["results"] = [{**result, "responses": result.get("rows") or [],
                                  "requestId": self._write_request_id, "batch": True,
                                  "cloids": [order.get("c") for order in orders]}]
        db.log_action(f"hl_{result['type']}", bool(result.get("live")), [result["action"]], [result])
        self._send_json(result)

    def _do_kraken_post(self, path, body):
        try:
            if path == "/api/arm":
                want = bool(body.get("armed"))
                challenge = str(body.get("challenge") or "")
                with arm_lock:
                    global armed
                    if want and not security.consume_arm_challenge(challenge):
                        self._send_json({
                            "armed": armed,
                            "needsConfirm": True,
                            "message": "A fresh one-time arming challenge is required.",
                        }, 403)
                        return
                    armed = want
                    state = armed
                aborting = chase_manager.abort_all() if not state else {"requested": [], "completed": [], "pending": []}
                sse.publish("armed", {"armed": state})
                hyperliquid_sse.publish("armed", {"armed": state})
                db.log_event("arm", {"armed": state, "env": "demo" if client.is_demo else "live", "abortingChases": aborting})
                self._send_json({"armed": state, "env": "demo" if client.is_demo else "live", "abortingChases": aborting})
            elif path == "/api/order":
                params, error = validate_order_params(body)
                if error:
                    self._send_json({"error": error}, 400)
                    return
                result = place_order(params)
                db.log_action("api_order", result.get("outcome") != "simulated", [{"type": "order", **params}], [result])
                self._send_json(result)
            elif path == "/api/cancel":
                result = cancel_order(body)
                db.log_action("api_cancel", result.get("outcome") != "simulated", [{"type": "cancel", **body}], [result])
                self._send_json(result)
            elif path in {"/api/grid/preview", "/api/grid"}:
                action = {key: body[key] for key in (
                    "symbol", "side", "startPrice", "endPrice", "orders", "size", "notional", "orderType", "reduceOnly", "previewHash",
                ) if key in body}
                action["type"] = "ladder"
                if "startPrice" not in action or "endPrice" not in action:
                    self._send_json({"error": "Grid requires start and end prices."}, 400)
                    return
                with arm_lock:
                    is_armed = armed
                    if path == "/api/grid/preview":
                        try:
                            plan = trading_actions._ladder_plan(action, action_ctx)
                            validation_error = None
                            try:
                                plan["warnings"] += trading_actions.validate_grid(plan, action_ctx)
                            except (trading_actions.ActionError, trading_actions.GridError) as exc:
                                validation_error = str(exc)
                        except (trading_actions.ActionError, trading_actions.GridError) as exc:
                            self._send_json({"error": str(exc)}, 400)
                            return
                        self._send_json({"plan": plan, "armed": is_armed,
                                         "ready": validation_error is None, "validationError": validation_error})
                        return
                    if not action.get("previewHash") or body.get("expectedArmed") is not is_armed:
                        self._send_json({"error": "Preview the grid again; ARM state or preview is missing/changed."}, 409)
                        return
                    cache.drop("positions", "orders", "account")
                    results = trading_actions.execute_actions([action], action_ctx, is_armed)
                    db.log_action("grid", is_armed, [action], results)
                cache.drop("positions", "orders", "account")
                self._send_json({"results": results, "armed": is_armed})
            elif path == "/api/action":
                raw = body.get("actions")
                try:
                    normalized = trading_actions.normalize_actions(raw)
                except trading_actions.ActionError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                if body.get("messageId") is not None:
                    try:
                        message_id = int(body["messageId"])
                        block_index = int(body.get("blockIndex") or 0)
                    except (TypeError, ValueError):
                        self._send_json({"error": "invalid proposal identity"}, 400)
                        return
                    session = db.ensure_session()
                    if not db.claim_action_proposal(session["id"], message_id, block_index, raw):
                        self._send_json({"error": "proposal already executed, missing, or changed; refresh chat"}, 409)
                        return
                with arm_lock:
                    is_armed = armed
                    results = trading_actions.execute_actions(normalized, action_ctx, is_armed)
                cache.drop("positions", "orders", "account")
                db.log_action("api", is_armed, normalized, results)
                self._send_json({"actions": normalized, "results": results, "armed": is_armed})
            elif path == "/api/flatten":
                mode = str(body.get("mode") or "").strip().lower()
                if mode not in {"emergency", "chase"}:
                    self._send_json({"error": "mode must be emergency or chase"}, 400)
                    return
                symbol = str(body.get("symbol") or "").strip().upper()
                result = flatten_all(mode, symbol)
                logged_results = result.get("results") or [{
                    "type": "flatten", "outcome": result.get("outcome"), "error": result.get("error"),
                }]
                db.log_action(f"flatten_{mode}", bool(result.get("armed")), result.get("actions") or [], logged_results)
                self._send_json(result)
            elif path == "/api/chat":
                self._handle_chat(body)
            elif path == "/api/chase":
                symbol = str(body.get("symbol", "")).strip().upper()
                side = str(body.get("side", "")).strip().lower()
                size = _as_float(body.get("size"))
                if not symbol.startswith("PF_") or side not in {"buy", "sell"} or not size or size <= 0:
                    self._send_json({"error": "PF_ symbol, side (buy|sell), and positive size required"}, 400)
                    return
                try:
                    spec = {
                        "symbol": symbol, "side": side, "size": size,
                        "reduceOnly": bool(body.get("reduceOnly")),
                        "timeoutSec": float(body.get("timeoutSec") or 300),
                        "maxRepegs": int(body.get("maxRepegs") or 120),
                        "repegSec": max(1.0, float(body.get("repegSec") or 5)),
                        "offsetTicks": max(0, int(body.get("offsetTicks") or 0)),
                    }
                except (TypeError, ValueError):
                    self._send_json({"error": "invalid Chase timing parameters"}, 400)
                    return
                if spec["timeoutSec"] <= 0 or spec["maxRepegs"] <= 0:
                    self._send_json({"error": "Chase timeoutSec and maxRepegs must be positive"}, 400)
                    return
                hub.watch([symbol])
                try:
                    snapshot = _start_chase_if_armed(spec)
                except trading_actions.ActionError as exc:
                    self._send_json({"error": str(exc)}, 400)
                    return
                self._send_json({"chase": snapshot})
            elif path == "/api/chase/abort":
                self._send_json(chase_manager.abort(str(body.get("chaseId", ""))))
            elif path == "/api/chat/note":
                # store-only message (e.g. execution reports) — never triggers the LLM
                content = str(body.get("content") or "").strip()
                if content:
                    session = db.ensure_session()
                    db.add_message(session["id"], str(body.get("role") or "assistant"), content[:8000],
                                   meta=body.get("meta") if isinstance(body.get("meta"), dict) else None)
                self._send_json({"ok": True})
            elif path == "/api/chat/reset":
                # wipe the conversation: messages out of context, summary cleared; memory file is kept
                session = db.ensure_session()
                db.reset_session(session["id"])
                db.log_event("chat_reset", {"session": session["id"]})
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "unknown endpoint"}, 404)
        except KrakenHTTPError as exc:
            self._send_json({"error": str(exc), "payload": exc.payload}, 502)
        except KrakenFuturesError as exc:
            self._send_json({"error": str(exc)}, 502)
        except ai_chat.ChatError as exc:
            self._send_json({"error": f"AI: {exc}"}, 502)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def _handle_chat(self, body: dict[str, Any]) -> None:
        message = str(body.get("message") or "").strip()
        legacy = body.get("messages")
        if not message and isinstance(legacy, list):
            last_user = [m for m in legacy if isinstance(m, dict) and m.get("role") == "user"]
            if last_user:
                message = str(last_user[-1].get("content", "")).strip()
        if not message:
            self._send_json({"error": "message required"}, 400)
            return
        message = message[:8000]

        symbol = str(body.get("symbol", "")).upper() or DEFAULT_WATCHLIST[0]
        last_execution = body.get("lastExecution")
        session = db.ensure_session()
        db.add_message(session["id"], "user", message)
        if isinstance(last_execution, dict) and last_execution:
            db.log_event("execution", last_execution)
        hub.watch([symbol])
        extra_syms = [s for s in _mentioned_symbols(message) if s != symbol]
        if extra_syms:
            hub.watch(extra_syms[:6])
        account_state = _account_payload(get_account())
        positions_state = _rows_payload("positions", get_positions())
        orders_state = _rows_payload("orders", get_orders())
        account = {key: value for key, value in account_state.items() if key not in {"state", "error", "ageSeconds"}}
        positions = positions_state["positions"]
        orders = orders_state["orders"]
        ticker = _ticker_payload(symbol)
        candles = hub.candles_1m(symbol)
        if not candles:
            candles = get_candles(symbol, "1m").get("candles", [])
        signal = None
        try:
            signal = scanner.ema_signal(client, symbol)
        except (KrakenFuturesError, ValueError):
            pass
        snapshot = ai_chat.build_context_snapshot(
            account=account,
            positions=positions,
            orders=orders,
            symbol=symbol,
            ticker=ticker,
            candles=candles,
            signal=signal,
        )
        unavailable = [
            f"{name} unavailable; showing last-known data"
            + (f" from {state['ageSeconds']}s ago" if state.get("ageSeconds") is not None else " only if present")
            + f"; error: {state.get('error')}"
            for name, state in (("account", account_state), ("positions", positions_state), ("orders", orders_state), ("market", ticker))
            if state.get("state") == "unavailable"
        ]
        if unavailable:
            snapshot += "\nDATA AVAILABILITY (AUTHORITATIVE): " + " | ".join(unavailable)
            snapshot += "\nDo not interpret unavailable data as an empty account, position list, or order list."
        with arm_lock:
            snapshot = f"TERMINAL: {'ARMED — write tools are LIVE' if armed else 'DISARMED — write tools simulate only'}\n" + snapshot
        try:
            inst = next(i for i in get_instruments().get("instruments", []) if i.get("symbol") == symbol)
            snapshot += f"\nSYMBOL META {symbol}: tickSize={inst.get('tickSize')} contractSize={inst.get('contractSize')} type={inst.get('type')} — ALL order prices must be rounded to this tickSize"
        except StopIteration:
            pass
        if isinstance(last_execution, dict) and last_execution:
            snapshot += "\nUI-REPORTED LAST EXECUTION (non-authoritative; verify against Kraken tools): " + json.dumps(last_execution, default=str)[:600]

        extra_lines = []
        for sym in extra_syms[:6]:
            t = _ticker_payload(sym)
            if t:
                extra_lines.append("MENTIONED " + sym + ": " + json.dumps(
                    {k: t.get(k) for k in ("last", "markPrice", "bid", "ask", "change24h", "fundingRate") if t.get(k) is not None}
                ))
        if extra_lines:
            snapshot += "\n" + "\n".join(extra_lines)

        limit = int(os.getenv("CHAT_CONTEXT_LIMIT", "200000"))
        history, session_memory, session_summary, estimated_tokens, compacted = chat_compaction.prepare_chat_context(
            db, session["id"], snapshot, limit,
        )
        if compacted:
            db.log_event("compaction", {
                "summarized": compacted,
                "estimatedPromptTokens": estimated_tokens,
                "phase": "pre_request",
            })
            sys.stderr.write(f"[compaction] summarized {compacted} messages before request\n")
        if history and history[-1]["role"] == "user":
            history[-1]["content"] = message  # guard against content truncation edge cases

        def audit_tool(entry: dict[str, Any]) -> None:
            with arm_lock:
                tool_armed = armed
            db.log_event("ai_tool", {
                "sessionId": session["id"],
                **entry,
                "armed": tool_armed,
                "args": json.dumps(entry.get("args"), default=str)[:4000],
            })

        result = ai_chat.respond(
            history, snapshot,
            session_memory=session_memory,
            session_summary=session_summary,
            tool_executor=chat_tool_exec,
            tool_audit=audit_tool,
        )
        result["messageId"] = db.add_message(
            session["id"], "assistant", result["text"],
            meta={"actionProposals": result.get("actionProposals", []), "orderProposals": result.get("orderProposals", [])},
        )

        # Latest prompt size is distinct from cumulative tokens billed across tool rounds.
        usage = result.get("usage") or {}
        db.record_model_usage(
            session["id"],
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("total_tokens") or 0),
        )

        # the AI maintains its own living memory file via update_memory
        if result.get("memory"):
            db.set_session_memory(session["id"], result["memory"])
            db.log_event("memory_update", {"chars": len(result["memory"])})

        self._send_json(result)





def _normalize_symbol(raw: str) -> str:
    sym = raw.strip().upper()
    if not sym:
        return ""
    if not sym.startswith("PF_"):
        sym = "PF_" + sym
    if not (sym.endswith("USD") or sym.endswith("USDT")):
        sym += "USD"
    return sym


_INSTRUMENT_ALIASES: dict[str, str] = {}


def _mentioned_symbols(message: str) -> list[str]:
    """Symbols the user mentioned — explicit (PF_X) or fuzzy ('ena') — for snapshot injection."""
    found = set(re.findall(r"\bPF_[A-Z0-9]{2,8}\b", message.upper()))
    if not _INSTRUMENT_ALIASES:
        try:
            for inst in get_instruments().get("instruments", []):
                sym = str(inst.get("symbol") or "")
                if sym.startswith("PF_") and sym.endswith("USD"):
                    base = sym[3:-3].lower()
                    if base.isalpha() and len(base) >= 2:
                        _INSTRUMENT_ALIASES[base] = sym
        except KrakenFuturesError:
            pass
    text = " " + message.lower() + " "
    for alias, sym in _INSTRUMENT_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", text):
            found.add(sym)
    return sorted(found)


_CHAT_ACTION_TOOLS = {
    "place_order": "order",
    "place_ladder": "ladder",
    "close_position": "close",
    "replace_tp": "replace_tp",
    "replace_sl": "replace_sl",
    "cancel_order": "cancel",
    "cancel_all_for_symbol": "cancel_all",
}


def _perf_summary() -> dict:
    # Aggregate the account log for the AI's get_performance tool.
    rows = account_log.compact_rows(client)
    # calendar boundaries in local time, matching the Stats modal: midnight / Monday 00:00 / 1st of month
    lt = time.localtime()
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    start_of_day = int(midnight)
    start_of_week = int(midnight - lt.tm_wday * 86400)  # tm_wday: Monday=0
    start_of_month = int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
    cutoffs = {
        "today": start_of_day,
        "week": start_of_week,
        "month": start_of_month,
        "allTime": 0,
    }
    trade_infos = ("futures trade", "futures partial liquidation")

    def num(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def row_net(r):
        # Wallet effect, same rule as the Stats modal (frontend/src/stats.js).
        info = r.get("info")
        if info in trade_infos:
            return num(r.get("pnl")) + num(r.get("funding")) - num(r.get("fee")) - num(r.get("liqFee"))
        if info == "funding rate change":
            return num(r.get("funding"))
        if info == "interest payment":
            return -num(r.get("fee"))
        return 0.0

    def agg(cutoff):
        window = [r for r in rows if r.get("t", 0) >= cutoff]
        fills = [r for r in window if r.get("info") in trade_infos]
        closes = [r for r in fills if num(r.get("pnl")) != 0]
        liquidations = [r for r in fills if r.get("info") == "futures partial liquidation"]
        wins = [r for r in closes if num(r.get("pnl")) > 0]
        return {
            "net": round(sum(row_net(r) for r in window), 2),
            "pricePnlBeforeCosts": round(sum(num(r.get("pnl")) for r in fills), 2),
            "tradingFees": round(sum(num(r.get("fee")) for r in fills), 2),
            "liquidationPenalties": round(sum(num(r.get("liqFee")) for r in fills), 2),
            "liquidationsAllIn": round(sum(row_net(r) for r in liquidations), 2),
            "funding": round(sum(num(r.get("funding")) for r in window
                                 if r.get("info") in trade_infos or r.get("info") == "funding rate change"), 2),
            "closingFills": len(closes),
            "winRatePctBeforeFees": round(100 * len(wins) / len(closes), 1) if closes else None,
        }

    by_sym = {}
    for r in rows:
        if r.get("info") in trade_infos and r.get("contract"):
            c = r["contract"]
            by_sym[c] = by_sym.get(c, 0) + row_net(r)
    ranked = sorted(by_sym.items(), key=lambda kv: kv[1])
    fmt_list = lambda pairs: [{ "symbol": s, "pnl": round(v, 2)} for s, v in pairs]
    out = {tf: agg(c) for tf, c in cutoffs.items()}
    out["worstSymbols"] = fmt_list(ranked[:4])
    out["bestSymbols"] = fmt_list(ranked[-3:])
    return out

def chat_tool_exec(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Tool executor for the AI chat: reads live state, writes via trading_actions (ARM-gated)."""
    print(f"[chat-tool] {name} {json.dumps(args, default=str)[:200]}", flush=True)
    if name == "get_market_data":
        sym = _normalize_symbol(str(args.get("symbol", "")))
        if not sym:
            return {"error": "symbol required"}
        hub.watch([sym])
        t = _ticker_payload(sym)
        out = {
            "symbol": sym,
            "state": t.get("state"),
            "ageSeconds": t.get("ageSeconds"),
            "ticker": {k: t.get(k) for k in ("last", "markPrice", "bid", "ask", "open24h", "high24h", "low24h", "change24h", "fundingRate", "openInterest") if t.get(k) is not None},
            **({"error": t["error"]} if t.get("error") else {}),
        }
        try:
            ob = client.get("/orderbook", params={"symbol": sym})
            obb = ob.get("orderBook") or {}
            out["bookTop"] = {"bids": obb.get("bids", [])[:5], "asks": obb.get("asks", [])[:5]}
        except Exception as exc:
            out["bookState"] = "unavailable"
            out["bookError"] = str(exc)
        candles = hub.candles_1m(sym, limit=60)
        if candles:
            out["recent1m"] = {
                "lastClose": candles[-1][4],
                "high60m": max(c[2] for c in candles),
                "low60m": min(c[3] for c in candles),
            }
        return out
    if name == "get_positions":
        return _rows_payload("positions", get_positions())
    if name == "get_account":
        return _account_payload(get_account())
    if name == "get_orders":
        return _rows_payload("orders", get_orders())
    if name == "get_fills":
        sym = _normalize_symbol(str(args.get("symbol") or ""))
        fills = get_fills()
        if sym:
            fills = [f for f in fills if isinstance(f, dict) and f.get("symbol") == sym]
        return {"fills": fills[:20]}
    if name == "get_instrument":
        sym = str(args.get("symbol", "")).strip().upper()
        inst = next((i for i in get_instruments().get("instruments", []) if i.get("symbol") == sym), None)
        if not inst:
            return {"error": f"unknown instrument {sym}"}
        return {k: inst.get(k) for k in ("symbol", "type", "tickSize", "contractSize", "maxLeverage", "isin")}
    if name == "get_performance":
        try:
            return _perf_summary()
        except Exception as exc:
            return {"error": str(exc)}
    if name == "get_chases":
        rows = chase_manager.list()
        if args.get("runningOnly"):
            rows = [row for row in rows if row.get("status") == "running"]
        return {"chases": [{**row, "events": (row.get("events") or [])[-4:]} for row in rows[:10]]}
    if name == "get_trade_history":
        sym = _normalize_symbol(str(args.get("symbol") or ""))
        if not sym:
            return {"error": "symbol required"}
        limit = max(1, min(50, int(args.get("limit") or 20)))
        refresh = bool(args.get("refresh", True))
        history = account_log.trade_history(client, sym, limit + 1, force=refresh)
        rows = history[:limit]
        return {
            "symbol": sym,
            "count": len(rows),
            "truncated": len(history) > limit,
            "refreshed": refresh,
            "asOfEpochMs": int(time.time() * 1000),
            "totalsScope": "returnedExecutions",
            "totals": {
                "realizedPnl": sum(row["realizedPnl"] for row in rows),
                "realizedFunding": sum(row["realizedFunding"] for row in rows),
                "fees": sum(row["fee"] for row in rows),
                "netPnl": sum(row["netPnl"] for row in rows),
            },
            "executions": rows,
        }
    if name == "scan_markets":
        return scanner.scan_volatility(
            client,
            window_minutes=max(3, min(60, int(args.get("windowMinutes") or 5))),
            limit=max(1, min(20, int(args.get("limit") or 10))),
            min_volume_quote=max(0.0, float(args.get("minVolumeQuote") or 1_000_000)),
            max_spread_percent=max(0.01, min(5.0, float(args.get("maxSpreadPercent") or 0.5))),
        )
    if name in {"get_grids", "move_grid"}:
        symbol = _normalize_symbol(str(args.get("symbol") or ""))
        side = args.get("side", "buy")
        if name == "get_grids":
            return {"grids": grid_move.list_grids(db, action_ctx, symbol, side)[:20]}
        with arm_lock:
            armed_now = armed
            try:
                result = grid_move.move_grid(db, action_ctx, {**args, "symbol": symbol}, armed_now)
            except grid_move.GridError as exc:
                result = {"type": "move_grid", "outcome": "rejected", "error": str(exc)}
            except Exception as exc:
                result = {"type": "move_grid", "outcome": "unknown", "error": str(exc)}
            db.log_action("chat", armed_now, [{**args, "type": "move_grid"}], [result])
        cache.drop("orders", "positions", "account")
        return {"armed": armed_now, **result}
    kind = _CHAT_ACTION_TOOLS.get(name)
    if kind:
        a = dict(args)
        a["type"] = kind
        with arm_lock:
            armed_now = armed
            results = trading_actions.execute_actions([a], action_ctx, armed_now)
        cache.drop("positions", "orders", "account")
        db.log_action("chat", armed_now, [a], results)
        r = results[0] if results else {"error": "no result"}
        r.pop("response", None)  # raw kraken ack, too noisy for the model
        return {"armed": armed_now, **json.loads(json.dumps(r, default=str))}
    return {"error": f"unknown tool {name}"}



# ---- all-pairs market list for the sidebar --------------------------------
_marketlist_cache: dict[str, Any] = {"ts": 0.0, "rows": []}


def _marketlist() -> list[dict[str, Any]]:
    """Every tradeable PF_ pair with last/mark/24h change/24h volume (one public call, 5s cache)."""
    now = time.monotonic()
    if _marketlist_cache["rows"] and now - _marketlist_cache["ts"] < 5.0:
        return _marketlist_cache["rows"]
    payload = client.get("/tickers")
    tickers = payload.get("tickers", []) if isinstance(payload, dict) else []
    tradeable = {
        str(i.get("symbol"))
        for i in get_instruments().get("instruments", [])
        if i.get("tradeable") and str(i.get("symbol", "")).startswith("PF_")
    }
    rows = []
    for t in tickers:
        sym = str(t.get("symbol") or "")
        if sym not in tradeable:
            continue
        rows.append({
            "symbol": sym,
            "last": _as_float(t.get("last")),
            "mark": _as_float(t.get("markPrice")),
            "change24h": _as_float(t.get("change24h")),
            "vol24h": _as_float(t.get("volumeQuote")) or _as_float(t.get("volQuote")) or 0.0,
        })
    rows.sort(key=lambda r: r["symbol"])
    _marketlist_cache["ts"] = now
    _marketlist_cache["rows"] = rows
    return rows


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), TerminalHandler)
    server.daemon_threads = True
    print(f"Kraken Futures terminal: http://127.0.0.1:{PORT}  (env: {'demo' if client.is_demo else 'live'}, DISARMED)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        hub.stop()
        binance_klines.stop()
        if hyperliquid.feed:
            hyperliquid.feed.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
