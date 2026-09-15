import ast
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, MagicMock, patch
from urllib.error import URLError, HTTPError

import actions
from kraken_client import KrakenFuturesClient, KrakenFuturesError, KrakenHTTPError


class FlattenReadFailureTests(unittest.TestCase):
    def test_order_read_failure_does_not_block_closing_positions(self):
        # Load only these functions, never import server and start its live threads.
        tree = ast.parse(Path(__file__).with_name("server.py").read_text())
        tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"flatten_all", "_flatten_outcome"}]
        positions = [{"symbol": "PF_UNIUSD", "side": "long", "size": 10}]
        sent = []

        def post(path, params, **kwargs):
            self.assertEqual(path, "/sendorder")
            self.assertTrue(params["reduceOnly"])
            sent.append(path)
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": "closed"}}

        def orders():
            self.assertTrue(sent, "Emergency must close before requesting order state")
            return [{"error": "TLS handshake timeout"}]

        def after_action(action, result, armed):
            if action["type"] == "close" and result["ok"]:
                positions.clear()

        ctx = SimpleNamespace(client=SimpleNamespace(post=post), get_positions=lambda: positions,
                              get_orders=orders, instrument=lambda _: {"contractValueTradePrecision": 0},
                              after_action=after_action)
        namespace = {"Any": Any, "armed": True, "arm_lock": threading.RLock(), "cache": Mock(),
                     "trading_actions": actions, "action_ctx": ctx, "get_positions": ctx.get_positions,
                     "get_orders": orders, "chase_manager": SimpleNamespace(abort_all=lambda: {
                         "requested": [], "completed": [], "pending": []})}
        exec(compile(tree, "isolated_flatten", "exec"), namespace)
        result = namespace["flatten_all"]("emergency")
        self.assertEqual(result["closedPositionCount"], 1)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(result["cancelledOrderCount"], 0)
        self.assertIn("TLS handshake timeout", result["error"])
        self.assertEqual(sent, ["/sendorder"])

    def test_position_state_is_still_required(self):
        with self.assertRaises(actions.ActionError):
            actions.build_flatten_actions("emergency", [{"error": "offline"}], defer_order_validation=True)


class ReadRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = KrakenFuturesClient(api_key="fake", api_secret="dGVzdA==")
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.read.return_value = b'{"result":"success","openPositions":[]}'
        self.response.headers = {"Content-Type": "application/json"}
        self.enterContext(patch("kraken_client.time.sleep"))

    def test_transient_get_retries_with_fresh_nonce(self):
        with patch("kraken_client.request.urlopen", side_effect=[URLError("TLS timeout"), self.response]) as opening:
            self.assertEqual(self.client.get("/openpositions", private=True)["openPositions"], [])
        self.assertEqual(opening.call_count, 2)
        first, second = [call.args[0] for call in opening.call_args_list]
        self.assertNotEqual(first.get_header("Nonce"), second.get_header("Nonce"))

    def test_exhausted_get_is_not_reported_as_empty(self):
        with patch("kraken_client.request.urlopen", side_effect=TimeoutError("offline")) as opening:
            with self.assertRaises(KrakenFuturesError):
                self.client.get("/openpositions", private=True)
        self.assertEqual(opening.call_count, 2)

    def test_http_rejection_is_not_retried(self):
        error = HTTPError("https://example.test", 403, "Forbidden", {}, None)
        self.addCleanup(error.close)
        with patch("kraken_client.request.urlopen", side_effect=error) as opening:
            with self.assertRaises(KrakenHTTPError):
                self.client.get("/openpositions", private=True)
        self.assertEqual(opening.call_count, 1)

    def test_uncertain_post_is_never_retried(self):
        with patch("kraken_client.request.urlopen", side_effect=URLError("TLS timeout")) as opening:
            with self.assertRaises(KrakenFuturesError):
                self.client.post("/sendorder", params={}, private=True)
        self.assertEqual(opening.call_count, 1)


if __name__ == "__main__":
    unittest.main()
