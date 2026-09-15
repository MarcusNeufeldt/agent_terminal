import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import binance_ws


class FakeClock:
    """Monotonic clock that advances a fixed amount per reading."""

    def __init__(self, step: float = 10.0) -> None:
        self.now = 1000.0
        self.step = step

    def monotonic(self) -> float:
        self.now += self.step
        return self.now

    def time(self) -> float:
        return self.now


class FakeConn:
    def __init__(self, messages=None) -> None:
        self.messages = list(messages or [])
        self.sent = []
        self.closed = False

    def send_json(self, payload) -> None:
        self.sent.append(payload)

    def recv_json(self):
        if not self.messages:
            raise socket.timeout("no data")
        item = self.messages.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def kline(symbol="NEARUSDT", close="2.40", closed=False):
    return {
        "e": "kline",
        "s": symbol,
        "k": {"t": 1789493880000, "o": "2.41", "h": "2.42", "l": "2.40",
              "c": close, "v": "100", "x": closed},
    }


class BinanceKlineStreamTests(unittest.TestCase):
    def setUp(self):
        self.published = []
        self.hub = SimpleNamespace(watchlist=lambda: ["PF_NEARUSD"])
        self.stream = binance_ws.BinanceKlineStream(
            self.hub, lambda event, payload: self.published.append((event, payload))
        )

    def test_session_gives_up_when_no_data_arrives_so_run_can_reconnect(self):
        """A half-open socket raises nothing and delivers nothing. Without a
        staleness check the session loops forever and the chart silently freezes."""
        conn = FakeConn()  # every recv times out
        clock = FakeClock(step=10.0)
        with patch.object(binance_ws, "open_websocket", return_value=conn), \
                patch.object(binance_ws, "time", clock):
            self.stream._session()
        self.assertTrue(conn.closed, "a stalled session must close its connection")
        self.assertEqual(self.published, [])

    def test_session_keeps_running_while_klines_flow(self):
        messages = [kline(), kline(close="2.41"), kline(close="2.42")]
        conn = FakeConn(messages)
        clock = FakeClock(step=1.0)

        published = []

        def publish(event, payload):
            published.append((event, payload))
            if len(published) >= 3:
                self.stream._stop.set()

        self.stream.publish = publish
        with patch.object(binance_ws, "open_websocket", return_value=conn), \
                patch.object(binance_ws, "time", clock), \
                patch.object(binance_ws, "THROTTLE", 0.0):
            self.stream._session()
        self.assertEqual(len(published), 3)
        self.assertEqual(published[0][0], "bcandle")
        self.assertEqual(published[0][1]["symbol"], "PF_NEARUSD")

    def test_subscribes_to_watchlist_symbols(self):
        conn = FakeConn()
        clock = FakeClock(step=10.0)
        with patch.object(binance_ws, "open_websocket", return_value=conn), \
                patch.object(binance_ws, "time", clock):
            self.stream._session()
        self.assertTrue(conn.sent, "the session must subscribe before reading")
        self.assertEqual(conn.sent[0]["method"], "SUBSCRIBE")
        self.assertIn("nearusdt@kline_1m", conn.sent[0]["params"])

    def test_status_reports_a_stalled_feed(self):
        """Without this the failure is invisible: the socket looks fine and the
        only symptom is a chart that stops moving."""
        self.assertFalse(self.stream.status()["connected"])
        conn = FakeConn()
        clock = FakeClock(step=10.0)
        with patch.object(binance_ws, "open_websocket", return_value=conn), \
                patch.object(binance_ws, "time", clock):
            self.stream._session()
        status = self.stream.status()
        self.assertFalse(status["connected"])
        self.assertIn("lastMessageAge", status)


if __name__ == "__main__":
    unittest.main()
