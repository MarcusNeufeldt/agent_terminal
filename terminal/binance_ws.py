"""Binance USDT-M kline websocket — live candle stream for charts (read-only).

One connection (vendored ws_vendored stack) shared by every symbol in the hub
watchlist; subscriptions follow hub.watch() dynamically. Emits throttled SSE
"bcandle" events: {symbol (kraken), t (sec), o, h, l, c, v, closed}. Candle
history comes from REST (binance_candles); this module only feeds the live
edge. Trading stays on Kraken.

NOTE: since Binance's 2026-04 WS architecture split, klines live under
/market/ws — the legacy /ws and /stream routes still complete the handshake
but push no data.
"""

from __future__ import annotations

import socket
import threading
import time

import binance_candles
from ws_vendored import WebSocketEndpoint, open_websocket

HOST = "fstream.binance.com"
PATH = "/market/ws"
THROTTLE = 0.5  # seconds between updates per symbol (closed bars always pass)
# A half-open socket raises nothing and delivers nothing, so elapsed time since the
# last kline is the only reliable liveness signal. Subscribed symbols push on every
# trade and close a bar every 60s, so silence this long means the session is dead.
STALE_AFTER = 90.0


def _to_kraken_symbol(binance_symbol: str) -> str:
    base = binance_symbol[:-4] if binance_symbol.endswith("USDT") else binance_symbol
    if base == "BTC":
        base = "XBT"
    return "PF_" + base + "USD"


class BinanceKlineStream:
    def __init__(self, hub, publish) -> None:
        self.hub = hub
        self.publish = publish
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connected = False
        self._last_message_ts: float | None = None
        self._last_error: str | None = None

    def status(self) -> dict:
        """Reported by /api/health. Without this a stalled feed is invisible: the
        socket looks healthy and the only symptom is a chart that stops moving."""
        with self._lock:
            age = None if self._last_message_ts is None else time.time() - self._last_message_ts
            return {
                "connected": self._connected,
                "lastMessageAge": None if age is None else round(age, 1),
                "stale": bool(age is not None and age > STALE_AFTER),
                "error": self._last_error,
            }

    def start(self) -> None:
        threading.Thread(target=self._run, name="binance-klines", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as exc:  # keep reconnecting, but stop hiding why
                with self._lock:
                    self._last_error = repr(exc)
            finally:
                with self._lock:
                    self._connected = False
            self._stop.wait(3.0)

    def _session(self) -> None:
        endpoint = WebSocketEndpoint(HOST, 443, True, PATH)
        conn = open_websocket(endpoint, timeout=10.0)
        subscribed: set[str] = set()
        last_sent: dict[str, float] = {}
        req_id = 0
        last_kline = time.monotonic()
        with self._lock:
            self._connected = True
            self._last_message_ts = time.time()
            self._last_error = None
        try:
            while not self._stop.is_set():
                wanted = set()
                for sym in self.hub.watchlist():
                    b = binance_candles.to_binance_symbol(sym)
                    if b:
                        wanted.add(b)
                missing = wanted - subscribed
                if missing:
                    req_id += 1
                    conn.send_json({
                        "method": "SUBSCRIBE",
                        "params": [s.lower() + "@kline_1m" for s in sorted(missing)],
                        "id": req_id,
                    })
                    subscribed |= missing
                try:
                    msg = conn.recv_json()
                except (socket.timeout, TimeoutError):
                    # Periodic wakeup: re-sync subscriptions; pongs keep us alive.
                    # But a dead connection times out forever without ever raising,
                    # so bail out and let _run reconnect instead of spinning silently.
                    if time.monotonic() - last_kline > STALE_AFTER:
                        with self._lock:
                            self._last_error = "no kline data for %.0fs" % STALE_AFTER
                        return
                    continue
                # /market/ws pushes raw events; tolerate a combined wrapper too
                if isinstance(msg, dict) and msg.get("e") == "kline":
                    data = msg
                elif isinstance(msg, dict) and isinstance(msg.get("data"), dict):
                    data = msg["data"]
                else:
                    continue
                if data.get("e") != "kline":
                    continue
                k = data.get("k") or {}
                bsym = str(data.get("s") or "")
                ts = int(k.get("t") or 0) // 1000
                if not bsym or not ts:
                    continue
                # Liveness is recorded before the throttle, so suppressed updates
                # still prove the connection is delivering.
                last_kline = time.monotonic()
                with self._lock:
                    self._last_message_ts = time.time()
                closed = bool(k.get("x"))
                now = time.monotonic()
                if not closed and now - last_sent.get(bsym, 0.0) < THROTTLE:
                    continue
                last_sent[bsym] = now
                self.publish("bcandle", {
                    "symbol": _to_kraken_symbol(bsym),
                    "t": ts,
                    "o": float(k.get("o") or 0.0),
                    "h": float(k.get("h") or 0.0),
                    "l": float(k.get("l") or 0.0),
                    "c": float(k.get("c") or 0.0),
                    "v": float(k.get("v") or 0.0),
                    "closed": closed,
                })
        finally:
            # The session owns the connection, so it clears the flag too: returning
            # on staleness must not leave the feed reported as connected.
            with self._lock:
                self._connected = False
            try:
                conn.close()
            except Exception:
                pass
