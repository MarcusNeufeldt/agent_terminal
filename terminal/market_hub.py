"""Market data hub: one upstream Kraken public websocket, fanned out to SSE clients.

Subscribes ticker + trade feeds for the active watchlist. Keeps latest tickers,
recent trades, and rolling 1m candles per symbol so the chart still works if the
public charts REST endpoint misbehaves. All upstream reads use strict timeouts
and the connection loop reconnects with backoff.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kraken_client import DEFAULT_LIVE_BASE_URL  # noqa: E402

WS_PATH = "/ws/v1"
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

DEFAULT_WATCHLIST = [
    "PI_XBTUSD", "PI_ETHUSD", "PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD",
    "PF_XRPUSD", "PF_DOGEUSD", "PF_ADAUSD", "PF_LTCUSD", "PF_LINKUSD",
    "PF_TRUMPUSD", "PF_XLMUSD",
]

TICKER_FIELD_MAP = {
    # ws name -> normalized (REST-style) name
    "last": "last",
    "markPrice": "markPrice",
    "bid": "bid",
    "ask": "ask",
    "bid_size": "bidSize",
    "ask_size": "askSize",
    "volume": "vol24h",
    "volumeQuote": "volumeQuote",
    "open": "open24h",
    "high": "high24h",
    "low": "low24h",
    "change": "change24h",
    "funding_rate": "fundingRate",
    "funding_rate_prediction": "fundingRatePrediction",
    "openInterest": "openInterest",
    "index": "indexPrice",
    "suspended": "suspended",
}


def _import_upstream():
    """Reuse the CLI's hand-rolled websocket client if available, else vendored copy."""
    cli_path = r"F:\explore\kraken-futures-cli"
    if Path(cli_path).exists():
        if cli_path not in sys.path:
            sys.path.insert(0, cli_path)
        try:
            from kraken_futures_cli import websocket as up  # noqa: PLC0415

            return up.open_websocket, up.WebSocketConnection, up.websocket_endpoint_from_base_url
        except Exception:
            pass
    from ws_vendored import (  # noqa: PLC0415
        open_websocket,
        websocket_endpoint_from_base_url,
    )

    return open_websocket, WebSocketConnection, websocket_endpoint_from_base_url  # type: ignore[name-defined]


class MarketHub:
    """Thread-safe market data hub with a broadcast callback per message."""

    def __init__(self, base_url: str = DEFAULT_LIVE_BASE_URL, on_message: Callable[[str, dict], None] | None = None):
        self.base_url = base_url
        self.on_message = on_message or (lambda kind, payload: None)
        self._lock = threading.Lock()
        self._tickers: dict[str, dict[str, Any]] = {}
        self._trades: dict[str, deque] = {}
        self._candles_1m: dict[str, deque] = {}  # [time_sec, open, high, low, close, vol]
        self._wanted: set[str] = set(DEFAULT_WATCHLIST)
        self._conn = None
        self._status = "connecting"
        self._stop = threading.Event()
        self._subscribed: set[str] = set()
        self._send_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name="market-hub", daemon=True)
        self._thread.start()

    # ---- public API -------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self._status,
                "symbols": sorted(self._subscribed),
            }

    def ticker(self, symbol: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._tickers.get(symbol)
            return dict(data) if data else None

    def tickers(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {sym: dict(data) for sym, data in self._tickers.items()}

    def recent_trades(self, symbol: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            trades = list(self._trades.get(symbol, []))
        return trades[-limit:]

    def candles_1m(self, symbol: str, limit: int = 720) -> list[list[float]]:
        """Completed + current 1m candles: [time, open, high, low, close, vol]."""
        with self._lock:
            candles = list(self._candles_1m.get(symbol, []))
        return candles[-limit:]

    def watch(self, symbols: list[str]) -> None:
        changed = False
        with self._lock:
            for sym in symbols:
                sym = sym.strip().upper()
                if sym and sym not in self._wanted:
                    self._wanted.add(sym)
                    changed = True
            wanted = set(self._wanted)
        if changed:
            self._sync_subscriptions(wanted)

    def watchlist(self) -> list[str]:
        with self._lock:
            return sorted(self._wanted)

    def stop(self) -> None:
        self._stop.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ---- internals --------------------------------------------------------

    def _send(self, conn, payload: dict[str, Any]) -> None:
        with self._send_lock:
            conn.send_json(payload)

    def _sync_subscriptions(self, wanted: set[str]) -> None:
        conn = self._conn
        if conn is None:
            return
        to_add = wanted - self._subscribed
        if not to_add:
            return
        try:
            self._send(conn, {"event": "subscribe", "feed": "ticker", "product_ids": sorted(to_add)})
            self._send(conn, {"event": "subscribe", "feed": "trade", "product_ids": sorted(to_add)})
            self._subscribed |= to_add
        except Exception:
            pass

    def _run(self) -> None:
        open_websocket, _, endpoint_from_base_url = _import_upstream()
        endpoint = endpoint_from_base_url(self.base_url)
        backoff = 1.0
        while not self._stop.is_set():
            try:
                with self._lock:
                    self._status = "connecting"
                    self._subscribed = set()
                conn = open_websocket(endpoint, timeout=10.0)
                self._conn = conn
                with self._lock:
                    self._status = "connected"
                    wanted = set(self._wanted)
                self._subscribed = set()
                self._send(conn, {"event": "subscribe", "feed": "ticker", "product_ids": sorted(wanted)})
                self._send(conn, {"event": "subscribe", "feed": "trade", "product_ids": sorted(wanted)})
                self._subscribed = set(wanted)
                self._push_status()
                backoff = 1.0

                conn.sock.settimeout(15.0)
                while not self._stop.is_set():
                    try:
                        msg = conn.recv_json()
                    except Exception:
                        raise
                    self._handle(msg)
            except Exception as exc:
                if self._stop.is_set():
                    break
                with self._lock:
                    self._status = f"reconnecting ({type(exc).__name__})"
                self._push_status()
                self._conn = None
                time.sleep(min(backoff, 15.0))
                backoff = min(backoff * 2, 15.0)
            finally:
                conn = self._conn
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    self._conn = None

    def _push_status(self) -> None:
        self.on_message("status", self.status())

    def _handle(self, msg: dict[str, Any]) -> None:
        feed = msg.get("feed")
        if feed == "ticker":
            symbol = msg.get("product_id") or msg.get("symbol")
            if not symbol:
                return
            snap = {out: msg[ws] for ws, out in TICKER_FIELD_MAP.items() if msg.get(ws) is not None}
            snap["symbol"] = symbol
            snap["time"] = time.time()
            last_v, open_v = snap.get("last"), snap.get("open24h")
            if snap.get("change24h") is None and last_v and open_v:
                try:
                    snap["change24h"] = (float(last_v) / float(open_v) - 1.0) * 100.0
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
            with self._lock:
                self._tickers[symbol] = snap
            self.on_message("ticker", snap)
        elif feed == "trade":
            symbol = msg.get("product_id")
            if not symbol:
                return
            try:
                price = float(msg.get("price"))
                qty = float(msg.get("qty", 0))
                ts_raw = msg.get("time")
                if isinstance(ts_raw, (int, float)):
                    ts = float(ts_raw)
                    if ts > 1e12:  # trade feed sends milliseconds
                        ts /= 1000.0
                else:
                    from datetime import datetime, timezone

                    ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                return
            trade = {"symbol": symbol, "price": price, "qty": qty, "side": msg.get("side"), "time": ts}
            with self._lock:
                buf = self._trades.setdefault(symbol, deque(maxlen=1000))
                buf.append(trade)
                self._update_candle(symbol, price, qty, int(ts // 60) * 60)
            self.on_message("trade", trade)

    def _update_candle(self, symbol: str, price: float, qty: float, bucket: int) -> None:
        candles = self._candles_1m.setdefault(symbol, deque(maxlen=1500))
        if candles and candles[-1][0] == bucket:
            c = candles[-1]
            c[2] = max(c[2], price)
            c[3] = min(c[3], price)
            c[4] = price
            c[5] += qty
        else:
            candles.append([bucket, price, price, price, price, qty])


def _self_test() -> None:
    hub = MarketHub()
    seen = {"ticker": 0, "trade": 0}
    hub.on_message = lambda kind, payload: seen.__setitem__(kind, seen[kind] + 1)
    hub.watch(["PI_XBTUSD"])
    deadline = time.time() + 12
    while time.time() < deadline and (seen["ticker"] < 2 or seen["trade"] < 1):
        time.sleep(0.3)
    t = hub.ticker("PI_XBTUSD")
    assert t and t.get("last"), f"no ticker received: {hub.status()}"
    print("ticker OK:", {k: t.get(k) for k in ("symbol", "last", "bid", "ask")})
    c = hub.candles_1m("PI_XBTUSD")
    assert c, "no candles built from trades"
    print("candles OK:", len(c), "last:", c[-1])
    hub.stop()
    print("self-test passed")


if __name__ == "__main__":
    _self_test()
