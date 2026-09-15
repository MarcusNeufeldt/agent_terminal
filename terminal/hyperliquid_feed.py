"""One lazy public Hyperliquid websocket, independent of the Kraken market hub."""

import threading
import time
from ws_vendored import WebSocketEndpoint, open_websocket


class HyperliquidFeed:
    def __init__(self, host, on_message, *, connector=open_websocket):
        self.host = host
        self.on_message = on_message
        self.connector = connector
        self._lock = threading.Lock()
        self._watch = {}
        self._thread = None
        self._stop = threading.Event()
        self._connection = None
        self._status = "idle"

    def watch(self, coins):
        with self._lock:
            now = time.monotonic()
            self._watch = {coin: seen for coin, seen in self._watch.items() if now - seen < 300}
            if len(set(coins) | self._watch.keys()) > 64:
                raise ValueError("Hyperliquid live feed supports up to 64 recently viewed markets")
            self._watch.update({coin: now for coin in coins})
            if self._thread is None or not self._thread.is_alive():
                self._stop = threading.Event()
                self._thread = threading.Thread(target=self._run, name="hyperliquid-public", daemon=True)
                self._thread.start()

    def status(self):
        with self._lock:
            return {"status": self._status, "exchange": "hyperliquid"}

    def stop(self):
        self._stop.set()
        with self._lock:
            connection = self._connection
        if connection:
            connection.close()
        if self._thread:
            self._thread.join(timeout=2)

    def _set_status(self, status):
        with self._lock:
            self._status = status
        self.on_message({"channel": "status", "data": self.status()})

    def _run(self):
        while not self._stop.is_set():
            connection = None
            try:
                self._set_status("connecting")
                connection = self.connector(WebSocketEndpoint(self.host, 443, True, "/ws"), timeout=12)
                with self._lock:
                    self._connection = connection
                subscribed = set()
                last_ping = time.monotonic()
                self._set_status("connected")
                while not self._stop.is_set():
                    now = time.monotonic()
                    with self._lock:
                        coins = {coin for coin, seen in self._watch.items() if now - seen < 300}
                    wanted = {(kind, coin) for coin in coins for kind in ("trades", "activeAssetCtx", "candle")}
                    for method, targets in (("unsubscribe", subscribed - wanted), ("subscribe", wanted - subscribed)):
                        for kind, coin in sorted(targets):
                            subscription = {"type": kind, "coin": coin}
                            if kind == "candle":
                                subscription["interval"] = "1m"
                            connection.send_json({"method": method, "subscription": subscription})
                    subscribed = wanted
                    if now - last_ping > 20:
                        connection.send_json({"method": "ping"})
                        last_ping = now
                    message = connection.recv_json()
                    if not self._stop.is_set():
                        self.on_message(message)
            except Exception:
                if not self._stop.is_set():
                    self._set_status("reconnecting")
            finally:
                if connection:
                    connection.close()
                with self._lock:
                    self._connection = None
            self._stop.wait(3)
        self._set_status("idle")
