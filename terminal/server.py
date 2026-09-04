"""Kraken Futures trading terminal server.

Stdlib-only HTTP server (ThreadingHTTPServer) serving:
  - static frontend from ./static
  - REST API (public market data, private account data, guarded order entry)
  - SSE stream /api/stream fanning out live tickers, trades, and hub status
  - AI chat POST /api/chat (OpenRouter, live account/market context)

Order safety: POST /api/order is SIMULATED until the terminal is armed via
POST /api/arm {"armed": true}. Armed state lives in memory only; restarting
the server always disarms.
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
import binance_ws as binance_ws_mod  # noqa: E402
from actions import ActionContext  # noqa: E402

STATIC_DIR = HERE / "static"

# Load configuration before reading PORT or constructing clients.
load_env_file(HERE / ".env")
load_env_file(Path(r"F:\explore\kraken-futures-cli") / ".env")
PORT = int(os.getenv("PORT", "8787"))

client = KrakenFuturesClient.from_env()
hub = MarketHub(client.base_url)

armed = False
arm_lock = threading.Lock()


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float) -> Any | None:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        return None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)

    def drop(self, *prefixes: str) -> None:
        with self._lock:
            for key in [k for k in self._data if k.startswith(prefixes)]:
                del self._data[key]


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


def get_account() -> dict[str, Any]:
    cached = cache.get("account", 3)
    if cached is not None:
        return cached
    try:
        payload = client.get("/accounts", private=True)
        account = extract_account(payload)
        cache.put("account", account)
        return account
    except KrakenFuturesError as exc:
        return {"error": str(exc)}


def get_positions() -> list[dict[str, Any]]:
    cached = cache.get("positions", 2)
    if cached is not None:
        return cached
    try:
        payload = client.get("/openpositions", private=True)
        positions = payload.get("openPositions", []) if isinstance(payload, dict) else []
        result = positions if isinstance(positions, list) else []
        result = enrich_positions(result)
        cache.put("positions", result)
        return result
    except KrakenFuturesError as exc:
        return [{"error": str(exc)}]


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
        orders = payload.get("openOrders", []) if isinstance(payload, dict) else []
        result = orders if isinstance(orders, list) else []
        cache.put("orders", result)
        return result
    except KrakenFuturesError as exc:
        return [{"error": str(exc)}]


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
        cache.put(f"ticker:{symbol}", match or {})
        return match
    except KrakenFuturesError:
        cache.put(f"ticker:{symbol}", {})
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
    get_positions=get_positions,
    get_orders=get_orders,
    get_instruments=get_instruments,
    get_ticker_rest=get_ticker_rest,
)
chase_manager = chase_mod.ChaseManager(sse.publish)
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

threading.Thread(target=_equity_snapshot_loop, name="equity-snapshots", daemon=True).start()
action_ctx.chase = chase_manager
action_ctx.after_action = _refresh_after_action
db = db_mod.Database()
_legacy_managed_protection_ids = db.inferred_managed_protection_ids()
_protection_sync_due: dict[tuple[Any, ...], float] = {}


def _managed_protection_loop() -> None:
    """Keep app-managed full-position TP/SL orders aligned with live position size."""
    while True:
        time.sleep(2.5)
        with arm_lock:
            is_armed = armed
        if not is_armed:
            _protection_sync_due.clear()
            continue
        try:
            candidates = trading_actions.managed_protection_sync_actions(
                get_positions(), get_orders(), _legacy_managed_protection_ids,
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

    if not symbol:
        return None, "symbol is required"
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
        is_armed = armed
    if not is_armed:
        return {
            "simulated": True,
            "message": "Terminal is DISARMED — order was not sent. Arm the terminal to trade live.",
            "params": params,
            "order": params,
        }
    response = client.post("/sendorder", params=params, private=True)
    cache.drop("positions", "orders", "account", "fills")
    sys.stderr.write("[order] " + json.dumps({"params": params, "sendStatus": response.get("sendStatus") or response}, default=str)[:400] + "\n")
    if isinstance(response, dict):
        send = response.get("sendStatus") or {}
        status = str(send.get("status") or "")
        events = send.get("orderEvents") or []
        reject_reason = next((str(e.get("reason")) for e in events if str(e.get("type")) == "REJECT" and e.get("reason")), None)
        if str(response.get("result")) != "success" or (status and status != "placed") or reject_reason:
            return {
                "error": f"Kraken rejected the order: {reject_reason or status or response.get('result')}",
                "response": response,
                "params": params,
                "order": params,
            }
    return {"simulated": False, "response": response, "params": params, "order": params}


def cancel_order(body: dict[str, Any]) -> dict[str, Any]:
    cli_ord_id = body.get("cliOrdId")
    order_id = body.get("orderId") or body.get("order_id")
    with arm_lock:
        is_armed = armed
    if not is_armed:
        return {"simulated": True, "message": "DISARMED — cancel not sent.", "target": cli_ord_id or order_id}
    if cli_ord_id:
        response = client.post("/cancelorder", params={"cliOrdId": str(cli_ord_id)}, private=True)
    elif order_id:
        response = client.post("/cancelorder", params={"order_id": str(order_id)}, private=True)
    else:
        return {"error": "cliOrdId or orderId required"}
    cache.drop("positions", "orders", "account")
    return {"simulated": False, "response": response}


# ---- HTTP handler ----------------------------------------------------------

class TerminalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KrakenFuturesTerminal/0.1"

    # ---- plumbing ----

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter logs
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1_000_000))
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        elif path == "/volatility":
            path = "/volatility.html"
        rel = path.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
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
            if path == "/api/health":
                with arm_lock:
                    is_armed = armed
                self._send_json({
                    "ok": True,
                    "armed": is_armed,
                    "env": "demo" if client.is_demo else "live",
                    "hub": hub.status(),
                    "hasKeys": bool(client.api_key and client.api_secret),
                    "aiModel": os.getenv("AI_CHAT_MODEL", ai_chat.DEFAULT_MODEL),
                })
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
                self._send_json(get_account())
            elif path == "/api/positions":
                self._send_json({"positions": get_positions()})
            elif path == "/api/orders":
                self._send_json({"orders": get_orders()})
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
            elif path == "/api/debug/threads":
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
        except KrakenHTTPError as exc:
            self._send_json({"error": str(exc), "status": exc.status, "payload": exc.payload}, 502)
        except KrakenFuturesError as exc:
            self._send_json({"error": str(exc)}, 502)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_sse(self) -> None:
        q = sse.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            hello = json.dumps({"status": hub.status(), "watchlist": hub.watchlist()})
            self.wfile.write(f"event: status\ndata: {hello}\n\n".encode("utf-8"))
            self.wfile.flush()
            idle = 0.0
            while True:
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
            sse.unsubscribe(q)

    # ---- POST ----

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()
        try:
            if path == "/api/arm":
                want = bool(body.get("armed"))
                confirm = str(body.get("confirm", "")).lower() == "yes"
                with arm_lock:
                    global armed
                    if want and not confirm:
                        self._send_json({
                            "armed": False,
                            "needsConfirm": True,
                            "message": "Live order entry. Type ARM in the confirm box to enable real orders on this account.",
                        })
                        return
                    armed = want
                    state = armed
                sse.publish("armed", {"armed": state})
                db.log_event("arm", {"armed": state, "env": "demo" if client.is_demo else "live"})
                self._send_json({"armed": state, "env": "demo" if client.is_demo else "live"})
            elif path == "/api/order":
                params, error = validate_order_params(body)
                if error:
                    self._send_json({"error": error}, 400)
                    return
                result = place_order(params)
                self._send_json(result)
            elif path == "/api/cancel":
                self._send_json(cancel_order(body))
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
            elif path == "/api/chat":
                self._handle_chat(body)
            elif path == "/api/chase":
                self._send_json({"error": "live Chase is temporarily disabled pending reconciliation hardening"}, 503)
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
        account = get_account()
        positions = [p for p in get_positions() if "error" not in p]
        orders = [o for o in get_orders() if "error" not in o]
        ticker = hub.ticker(symbol) or get_ticker_rest(symbol)
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
            t = hub.ticker(sym) or get_ticker_rest(sym)
            if t:
                extra_lines.append("MENTIONED " + sym + ": " + json.dumps(
                    {k: t.get(k) for k in ("last", "markPrice", "bid", "ask", "change24h", "fundingRate") if t.get(k) is not None}
                ))
        if extra_lines:
            snapshot += "\n" + "\n".join(extra_lines)

        history = [{"role": m["role"], "content": m["content"]} for m in db.get_messages(session["id"], limit=400)]
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
            session_memory=db.get_session_memory(session["id"]),
            session_summary=db.get_session_summary(session["id"]),
            tool_executor=chat_tool_exec,
            tool_audit=audit_tool,
        )
        result["messageId"] = db.add_message(
            session["id"], "assistant", result["text"],
            meta={"actionProposals": result.get("actionProposals", []), "orderProposals": result.get("orderProposals", [])},
        )

        # token tracking + threshold compaction (living memory file is never touched)
        try:
            usage = result.get("usage") or {}
            used = int(usage.get("total_tokens") or 0)
            if used:
                db.set_context_tokens(session["id"], used)
            limit = int(os.getenv("CHAT_CONTEXT_LIMIT", "200000"))
            if used and used > limit:
                to_compact = db.compact(session["id"], keep_last=20)
                if to_compact:
                    summary = ai_chat.summarize_for_compaction(to_compact, db.get_session_summary(session["id"]))
                    db.set_session_summary(session["id"], summary)
                    db.set_context_tokens(session["id"], int(used * 0.55))  # rough post-compaction estimate
                    db.log_event("compaction", {"summarized": len(to_compact), "tokens_before": used})
                    sys.stderr.write(f"[compaction] {len(to_compact)} messages summarized at {used} tokens\n")
        except Exception as exc:
            sys.stderr.write(f"[compaction] skipped: {exc}\n")

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

    def agg(cutoff):
        trades = [r for r in rows if r.get("info") in trade_infos and r.get("t", 0) >= cutoff and r.get("pnl") not in (None, 0)]
        pnl = sum(r.get("pnl") or 0 for r in trades)
        liq = sum(r.get("pnl") or 0 for r in trades if r.get("info") == "futures partial liquidation")
        funding = sum(r.get("funding") or 0 for r in rows if r.get("info") == "funding rate change" and r.get("t", 0) >= cutoff)
        wins = [r for r in trades if (r.get("pnl") or 0) > 0]
        wr = round(100 * len(wins) / len(trades), 1) if trades else None
        return {
            "net": round(pnl, 2),
            "liquidationLosses": round(liq, 2),
            "funding": round(funding, 2),
            "closingFills": len(trades),
            "winRatePct": wr,
        }

    by_sym = {}
    for r in rows:
        if r.get("info") in trade_infos and r.get("contract"):
            c = r["contract"]
            by_sym[c] = by_sym.get(c, 0) + (r.get("pnl") or 0)
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
        t = hub.ticker(sym) or get_ticker_rest(sym)
        if not t:
            return {"error": f"no market data for {sym} (unknown symbol?)"}
        out = {
            "symbol": sym,
            "ticker": {k: t.get(k) for k in ("last", "markPrice", "bid", "ask", "open24h", "high24h", "low24h", "change24h", "fundingRate", "openInterest") if t.get(k) is not None},
        }
        try:
            ob = client.get("/orderbook", params={"symbol": sym})
            obb = ob.get("orderBook") or {}
            out["bookTop"] = {"bids": obb.get("bids", [])[:5], "asks": obb.get("asks", [])[:5]}
        except Exception:
            pass
        candles = hub.candles_1m(sym, limit=60)
        if candles:
            out["recent1m"] = {
                "lastClose": candles[-1][4],
                "high60m": max(c[2] for c in candles),
                "low60m": min(c[3] for c in candles),
            }
        return out
    if name == "get_positions":
        return {"positions": [p for p in get_positions() if isinstance(p, dict) and "error" not in p]}
    if name == "get_account":
        acct = get_account()
        return acct if acct else {"error": "account unavailable (keys not configured?)"}
    if name == "get_orders":
        return {"orders": [o for o in get_orders() if isinstance(o, dict) and "error" not in o]}
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
        server.shutdown()


if __name__ == "__main__":
    main()
