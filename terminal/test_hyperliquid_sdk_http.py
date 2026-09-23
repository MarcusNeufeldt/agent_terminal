"""Real SDK HTTP path against a disposable loopback server. Never an exchange."""
import gzip
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hyperliquid.api import API
from hyperliquid_client import HyperliquidClient, HyperliquidError
from hyperliquid_sdk_http import SDKTransport
from hyperliquid_trading import ExchangeTransport, HyperliquidTrader


class SDKHTTPTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.status = 200
        self.body = b'{"status":"ok","response":{"type":"order","data":{"statuses":[{"resting":{"oid":123}}]}}}'
        self.headers = {}
        self.drop = False
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def do_POST(self):
                fixture.calls.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                if fixture.drop:
                    self.close_connection = True
                    return
                self.send_response(fixture.status)
                self.send_header("Content-Length", str(len(fixture.body)))
                for key, value in fixture.headers.items():
                    self.send_header(key, value)
                self.end_headers()
                try:
                    self.wfile.write(fixture.body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.http = SDKTransport(f"http://127.0.0.1:{self.server.server_port}", timeout=1, max_bytes=1024)
        self.exchange = ExchangeTransport("api.hyperliquid-testnet.xyz", transport=self.http)
        self.trader = HyperliquidTrader(lambda _: {"assetId": 1, "contractValueTradePrecision": 2},
            transport=self.exchange, private_key="1".zfill(64), account_address="0x" + "1" * 40, network="testnet")
        self.action = {"type": "order", "orders": [{"a": 1, "b": True, "p": "1", "s": "20", "r": False,
                       "t": {"limit": {"tif": "Ioc"}}, "c": "0x" + "a" * 32}], "grouping": "na"}

    def tearDown(self):
        self.http.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_exact_prepared_action_goes_through_sdk_once_with_signature(self):
        result = self.trader.submit(self.action)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(len(self.calls), 1)
        path, body = self.calls[0]
        self.assertEqual(path, "/exchange")
        self.assertEqual(body["action"], self.action)
        self.assertEqual(set(body), {"action", "nonce", "signature"})
        self.assertIsInstance(self.http._idle[-1], API)
        self.assertEqual(self.http._idle[-1].session.get_adapter("https://").max_retries.total, 0)

    def test_dropped_response_is_unknown_and_never_retried(self):
        self.drop = True
        result = self.trader.submit(self.action)
        self.assertEqual(result["outcome"], "unknown")
        self.assertTrue(result["uncertain"])
        self.assertEqual(len(self.calls), 1)

    def test_redirect_invalid_json_and_server_failure_never_confirm_or_retry(self):
        for status, body in ((302, b'{}'), (200, b'not-json'), (502, b'upstream failed')):
            with self.subTest(status=status):
                self.status, self.body = status, body
                self.headers = {"Location": f"http://127.0.0.1:{self.server.server_port}/redirect-target"}
                before = len(self.calls)
                self.assertEqual(self.trader.submit(self.action)["outcome"], "unknown")
                self.assertEqual(len(self.calls), before + 1)

    def test_plain_and_compressed_oversized_responses_are_bounded(self):
        for compressed in (False, True):
            data = b'x' * 2048
            self.body = gzip.compress(data) if compressed else data
            self.headers = {"Content-Encoding": "gzip"} if compressed else {}
            before = len(self.calls)
            self.assertEqual(self.trader.submit(self.action)["outcome"], "unknown")
            self.assertEqual(len(self.calls), before + 1)

    def test_sdk_info_preserves_bare_strings_and_rate_limit_cooldown(self):
        info = HyperliquidClient("testnet", transport=self.http)
        self.body = b'"unifiedAccount"'
        self.assertEqual(info.info("userAbstraction", user="0x" + "1" * 40), "unifiedAccount")
        self.assertEqual(self.calls[-1][0], "/info")
        self.status, self.body = 429, b'{"unexpected":"proxy shape without code/msg"}'
        self.headers = {"Retry-After": "120"}
        with self.assertRaisesRegex(HyperliquidError, "429"):
            info.info("recentTrades", coin="APT")
        before = len(self.calls)
        with self.assertRaisesRegex(HyperliquidError, "cooldown"):
            info.info("recentTrades", coin="APT")
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.trader.submit(self.action)["outcome"], "rejected")

    def test_concurrent_handlers_use_separate_sdk_sessions(self):
        # Hold both requests open at once: each must have checked out its own session.
        barrier = threading.Barrier(2)
        seen = []
        original = API.post
        def post(api, path, payload):
            seen.append(api.session)
            barrier.wait(timeout=2)
            return original(api, path, payload)
        API.post = post
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: self.http.post("/info", {"type": "metaAndAssetCtxs"}), range(2)))
        finally:
            API.post = original
        self.assertIsNot(seen[0], seen[1])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.http._idle), 2, "both sessions stay warm for the next requests")

    def test_the_next_request_reuses_a_warm_session_from_any_thread(self):
        self.http.post("/info", {"type": "allMids"})
        first = self.http._idle[-1]
        worker = threading.Thread(target=lambda: self.http.post("/info", {"type": "allMids"}))
        worker.start(); worker.join(timeout=2)
        self.assertEqual(self.http._idle, [first], "a new handler thread reuses the open connection")

    def test_a_failed_request_never_returns_its_session_to_the_pool(self):
        self.drop = True
        with self.assertRaises(Exception):
            self.http.post("/exchange", {"x": 1})
        self.assertEqual(self.http._idle, [])


if __name__ == "__main__":
    unittest.main()
