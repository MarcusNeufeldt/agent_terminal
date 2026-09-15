import base64
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import account_log
import ai_chat
import chat_compaction
import market_hub
import scanner
from actions import (
    MANAGED_TP_PREFIX, ActionContext, ActionError, _replace_protection_plan, build_flatten_actions,
    cancel_one, execute_actions, execute_emergency_flatten, managed_protection_sync_actions,
    normalize_actions, submit_one,
)
from chase import ChaseManager, ChaseRejected, ChaseTransient, ChaseUnknown, ChaseWorker
from db import Database
from exchange_ops import ensure_client_id, parse_operation
from kraken_client import KrakenFuturesClient, load_env_file
from local_security import LocalSecurity, safe_static_path
from read_state import account_payload, rows_payload


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class ProtectionContext:
    client = None

    def get_positions(self):
        return [{"symbol": "PF_EGLDUSD", "side": "short", "size": 644.41, "price": 4.668645955}]

    def get_orders(self):
        return [
            {"symbol": "PF_EGLDUSD", "side": "buy", "orderType": "take_profit", "reduceOnly": True,
             "order_id": "tp-1", "size": 644.41, "unfilledSize": 644.41, "stopPrice": 4.622, "triggerSignal": "mark"},
            {"symbol": "PF_EGLDUSD", "side": "buy", "orderType": "stop", "reduceOnly": True,
             "order_id": "sl-1", "size": 644.41, "unfilledSize": 644.41, "stopPrice": 4.715, "triggerSignal": "mark"},
        ]

    def instrument(self, _symbol):
        return {"tickSize": 0.001, "contractValueTradePrecision": 2}

    def mark_price(self, _symbol):
        return 4.65


class LongProtectionContext(ProtectionContext):
    def get_positions(self):
        return [{"symbol": "PF_TRUMPUSD", "side": "long", "size": 1482.8, "price": 2.18205171576562}]

    def get_orders(self):
        return []

    def mark_price(self, _symbol):
        return 2.413

    def current_price(self, _symbol):
        return 2.4

    def instrument(self, _symbol):
        return {"tickSize": 0.001, "contractValueTradePrecision": 1}


class FakeTradingClient:
    def post(self, path, **_kwargs):
        if path == "/sendorder":
            return {"result": "success", "sendStatus": {"status": "placed", "order_id": "fake-order"}}
        return {"result": "success"}


class SequentialProtectionContext(LongProtectionContext):
    def __init__(self):
        self.client = FakeTradingClient()
        self.size = 2471.2
        self.after_action = self._after_action

    def get_positions(self):
        return [{"symbol": "PF_TRUMPUSD", "side": "long", "size": self.size, "price": 2.18205171576562}]

    def _after_action(self, action, result, _armed):
        if action["type"] == "close" and result.get("ok"):
            self.size = result["remainingSize"]


class ProtectionScriptClient:
    def __init__(self, responses):
        self.responses = {path: list(values) for path, values in responses.items()}
        self.calls = []

    def post(self, path, params=None, **_kwargs):
        self.calls.append((path, params))
        value = self.responses[path].pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class ProtectionExecutionContext:
    def __init__(self, client, orders):
        self.client = client
        self.orders = orders
        self.alerts = []
        self.cleared = []
        self.after_action = None
        self.refresh_orders = lambda: None
        self.set_protection_alert = lambda symbol, kind, details: self.alerts.append((symbol, kind, details))
        self.clear_protection_alert = lambda symbol, kind: self.cleared.append((symbol, kind))

    def get_positions(self):
        return [{"symbol": "PF_TESTUSD", "side": "long", "size": 10, "price": 90}]

    def get_orders(self):
        return self.orders

    def instrument(self, _symbol):
        return {"tickSize": 0.1, "contractValueTradePrecision": 0}

    def mark_price(self, _symbol):
        return Decimal("100")


class ScannerClient:
    def __init__(self):
        self.ticker_calls = 0

    def get(self, _path):
        self.ticker_calls += 1
        base = {"tag": "perpetual", "suspended": False, "markPrice": 1, "bid": 0.999, "ask": 1.001, "volumeQuote": 2_000_000}
        return {"tickers": [{**base, "symbol": "PF_TESTUSD"}, {**base, "symbol": "PI_TESTUSD"}]}

    def get_public_charts(self, _symbol, _resolution, **_kwargs):
        minute = int(time.time() // 60) * 60_000
        return {"candles": [
            {"time": minute - (7 - i) * 60_000, "open": 1 + i / 1000, "high": 1.01 + i / 1000, "low": 0.99 + i / 1000, "close": 1 + i / 1000}
            for i in range(7)
        ]}


class RobustnessTests(unittest.TestCase):
    @staticmethod
    def _chase_worker(client):
        context = SimpleNamespace(
            client=client,
            get_instruments=lambda: {"instruments": [{"symbol": "PF_TESTUSD", "tickSize": 0.1, "contractValueTradePrecision": 0}]},
            get_ticker_rest=lambda _symbol: {"bid": 10, "ask": 11},
            hub=SimpleNamespace(ticker=lambda _symbol: {"bid": 10, "ask": 11}),
        )
        spec = {"symbol": "PF_TESTUSD", "side": "buy", "size": 10, "settlePollSec": 0, "visibilityGraceSec": 0}
        return ChaseWorker(spec, context, lambda *_args: None)

    def test_chase_rejects_nested_placement_failure(self):
        client = SimpleNamespace(post=lambda *_args, **_kwargs: {"result": "success", "sendStatus": {"status": "postWouldExecute"}})
        worker = self._chase_worker(client)
        worker.pegs = 1
        with self.assertRaises(ChaseRejected):
            worker._place(Decimal("10"), Decimal("10"))
        self.assertIsNone(worker._active)

    def test_reduce_only_chase_marks_every_exchange_order_reduce_only(self):
        calls = []
        client = SimpleNamespace(post=lambda path, params=None, **_kwargs: (
            calls.append((path, params)) or {"result": "success", "sendStatus": {"status": "placed", "order_id": "close-1"}}
        ))
        client.get = lambda *_args, **_kwargs: {"result": "success", "openPositions": [
            {"symbol": "PF_TESTUSD", "side": "short", "size": 10}]}
        worker = self._chase_worker(client)
        worker.spec["reduceOnly"] = True
        worker.pegs = 1
        worker._place(Decimal("10"), Decimal("10"))
        self.assertTrue(calls[0][1]["reduceOnly"])
        self.assertTrue(worker.snapshot()["spec"]["reduceOnly"])

    def test_chase_placed_without_exchange_id_is_unknown_and_not_retried(self):
        client = SimpleNamespace(post=lambda *_args, **_kwargs: {"result": "success", "sendStatus": {"status": "placed"}})
        worker = self._chase_worker(client)
        worker.pegs = 1
        with self.assertRaises(ChaseUnknown):
            worker._place(Decimal("10"), Decimal("10"))
        self.assertIsNotNone(worker._active)
        self.assertIsNone(worker._active["orderId"])

    def test_chase_verified_external_cancellation_stops_without_replacement(self):
        def get(path, **_kwargs):
            return {"result": "success", "openOrders": []} if path == "/openorders" else {"result": "success", "fills": []}

        client = SimpleNamespace(get=get, post=lambda *_args, **_kwargs: {
            "result": "success", "orders": [{"status": "CANCELLED", "order": {"orderId": "order-1", "filled": 0}}],
        })
        worker = self._chase_worker(client)
        worker._active = {"cliOrdId": "ch-test", "orderId": "order-1", "price": Decimal("10"), "size": 10, "seenFilled": 0, "placedAt": 0}
        self.assertFalse(worker._reconcile_resting())
        self.assertEqual(worker.status, "cancelled")
        self.assertEqual(worker.filled, 0)
        self.assertIsNone(worker._active)

    def test_chase_open_order_read_failure_leaves_order_untouched(self):
        def get(*_args, **_kwargs):
            raise RuntimeError("temporary read failure")

        worker = self._chase_worker(SimpleNamespace(get=get))
        worker._active = {"cliOrdId": "ch-test", "orderId": "order-1", "price": Decimal("10"), "size": 10, "seenFilled": 0, "placedAt": 0}
        with self.assertRaises(ChaseTransient):
            worker._reconcile_resting()
        self.assertIsNotNone(worker._active)

    def test_chase_cancel_failure_never_clears_active_order(self):
        def post(*_args, **_kwargs):
            raise RuntimeError("cancel timed out")

        worker = self._chase_worker(SimpleNamespace(post=post))
        worker._active = {"cliOrdId": "ch-test", "orderId": "order-1", "price": Decimal("10"), "size": 10, "seenFilled": 0, "placedAt": 0}
        with self.assertRaises(ChaseUnknown):
            worker._cancel_active()
        self.assertIsNotNone(worker._active)

    def test_chase_nested_cancel_failure_never_clears_active_order(self):
        client = SimpleNamespace(post=lambda *_args, **_kwargs: {
            "result": "success", "cancelStatus": {"status": "notFound"},
        })
        worker = self._chase_worker(client)
        worker._active = {"cliOrdId": "ch-test", "orderId": "order-1", "price": Decimal("10"), "size": 10, "seenFilled": 0, "placedAt": 0}
        with self.assertRaises(ChaseUnknown):
            worker._cancel_active()
        self.assertIsNotNone(worker._active)

    def test_chase_reconciles_late_fill_after_confirmed_cancel(self):
        def post(path, **_kwargs):
            if path == "/orders/status":
                return {"result": "success", "orders": [{
                    "status": "CANCELLED", "order": {"orderId": "order-1", "filled": 3},
                }]}
            return {"result": "success", "cancelStatus": {"status": "cancelled", "order_id": "order-1"}}

        def get(path, **_kwargs):
            if path == "/openorders":
                return {"result": "success", "openOrders": []}
            return {"result": "success", "fills": [{"order_id": "order-1", "size": 3}]}

        worker = self._chase_worker(SimpleNamespace(post=post, get=get))
        worker._active = {"cliOrdId": "ch-test", "orderId": "order-1", "price": Decimal("10"), "size": 10, "seenFilled": 1, "placedAt": 0}
        worker._cancel_active()
        self.assertEqual(worker.filled, 3)
        self.assertEqual(worker._base_filled, 3)
        self.assertIsNone(worker._active)
        self.assertEqual(worker.state, "RECONCILED")

    def test_chase_timeout_after_partial_fill_reports_partial(self):
        def post(path, **_kwargs):
            if path == "/orders/status":
                return {"result": "success", "orders": [{
                    "status": "CANCELLED", "order": {"orderId": "order-1", "filled": 3},
                }]}
            return {"result": "success", "cancelStatus": {"status": "cancelled"}}

        def get(path, **_kwargs):
            return {"result": "success", "openOrders": []} if path == "/openorders" else {"result": "success", "fills": [{"order_id": "order-1", "size": 3}]}

        worker = self._chase_worker(SimpleNamespace(post=post, get=get))
        worker._active = {"cliOrdId": "ch-test", "orderId": "order-1", "price": Decimal("10"), "size": 10, "seenFilled": 1, "placedAt": 0}
        worker._stop_after_cancel("timeout")
        self.assertEqual(worker.status, "partial")
        self.assertEqual(worker.stop_reason, "timeout")
        self.assertEqual(worker.filled, 3)

    def test_chase_full_execution_requires_authoritative_order_status(self):
        def post(path, **_kwargs):
            self.assertEqual(path, "/orders/status")
            return {"result": "success", "orders": [{
                "status": "FULLY_EXECUTED", "order": {"orderId": "order-1", "filled": 10},
            }]}

        def get(path, **_kwargs):
            return {"result": "success", "openOrders": []} if path == "/openorders" else {"result": "success", "fills": [{"order_id": "order-1", "size": 10}]}

        worker = self._chase_worker(SimpleNamespace(post=post, get=get))
        worker._active = {"cliOrdId": "ch-test", "orderId": "order-1", "price": Decimal("10"), "size": 10, "seenFilled": 0, "placedAt": 0}
        self.assertTrue(worker._reconcile_resting())
        self.assertEqual(worker.filled, 10)
        self.assertIsNone(worker._active)

    def test_chase_rejects_size_below_contract_lot_without_placing(self):
        client = Mock()
        worker = self._chase_worker(client)
        worker.ctx.get_instruments = lambda: {"instruments": [{
            "symbol": "PF_TESTUSD", "tickSize": 0.1, "contractValueTradePrecision": -2,
        }]}
        worker.spec["size"] = 10
        worker.run()
        self.assertEqual(worker.status, "rejected")
        client.post.assert_not_called()

    def test_chase_replaces_only_after_cancel_and_uses_reconciled_remaining_size(self):
        class Client:
            def __init__(self):
                self.send_count = 0
                self.open_count = 0
                self.write_calls = []

            def post(self, path, params=None, **_kwargs):
                if path == "/sendorder":
                    self.send_count += 1
                    self.write_calls.append(("send", params["size"]))
                    return {"result": "success", "sendStatus": {"status": "placed", "order_id": f"order-{self.send_count}"}}
                if path == "/cancelorder":
                    self.write_calls.append(("cancel", params["cliOrdId"]))
                    return {"result": "success", "cancelStatus": {"status": "cancelled"}}
                order_id = f"order-{self.send_count}"
                filled = 3 if order_id == "order-1" else 7
                status = "CANCELLED" if order_id == "order-1" else "FULLY_EXECUTED"
                return {"result": "success", "orders": [{"status": status, "order": {"orderId": order_id, "filled": filled}}]}

            def get(self, path, **_kwargs):
                if path == "/openorders":
                    self.open_count += 1
                    if self.open_count == 1:
                        return {"result": "success", "openOrders": [{"order_id": "order-1", "filledSize": 2, "unfilledSize": 8}]}
                    return {"result": "success", "openOrders": []}
                order_id = f"order-{self.send_count}"
                size = 3 if order_id == "order-1" else 7
                return {"result": "success", "fills": [{"order_id": order_id, "size": size}]}

        client = Client()
        worker = self._chase_worker(client)
        bids = iter((10, 9))
        worker.ctx.hub.ticker = lambda _symbol: {"bid": next(bids, 9), "ask": 11}
        worker.spec.update({"repegSec": 0.01, "timeoutSec": 2})
        worker.run()
        self.assertEqual(worker.status, "filled")
        self.assertEqual(client.write_calls[0], ("send", 10.0))
        self.assertEqual(client.write_calls[1][0], "cancel")
        self.assertEqual(client.write_calls[2], ("send", 7.0))

    def test_disarm_aborts_cancels_and_reconciles_running_worker(self):
        placed = threading.Event()

        class Client:
            def post(self, path, params=None, **_kwargs):
                if path == "/sendorder":
                    placed.set()
                    return {"result": "success", "sendStatus": {"status": "placed", "order_id": "order-1"}}
                if path == "/cancelorder":
                    return {"result": "success", "cancelStatus": {"status": "cancelled"}}
                return {"result": "success", "orders": [{"status": "CANCELLED", "order": {"orderId": "order-1", "filled": 0}}]}

            def get(self, path, **_kwargs):
                return {"result": "success", "openOrders": []} if path == "/openorders" else {"result": "success", "fills": []}

        manager = ChaseManager(lambda *_args: None)
        worker = self._chase_worker(Client())
        worker.spec.update({"repegSec": 1, "timeoutSec": 10})
        manager._chases[worker.id] = worker
        worker.start()
        self.assertTrue(placed.wait(1))
        report = manager.abort_all(wait_timeout=2)
        self.assertEqual(report["requested"], [worker.id])
        self.assertEqual(report["pending"], [])
        self.assertEqual(report["completed"][0]["status"], "aborted")

    def test_disarm_reports_a_worker_that_did_not_finish(self):
        manager = ChaseManager(lambda *_args: None)
        worker = Mock(status="running", id="active")
        worker.is_alive.return_value = True
        manager._chases["active"] = worker
        report = manager.abort_all(wait_timeout=0)
        self.assertEqual(report["requested"], ["active"])
        self.assertEqual(len(report["pending"]), 1)
        worker.abort.assert_called_once_with()

    def test_startup_orphan_detection_is_read_only_and_visible(self):
        published = []
        manager = ChaseManager(lambda kind, payload: published.append((kind, payload)))
        found = manager.detect_orphans([{
            "cliOrdId": "ch-old-1", "order_id": "order-1", "symbol": "PF_TESTUSD",
            "side": "buy", "filledSize": 2, "unfilledSize": 8,
        }])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["status"], "orphaned")
        self.assertEqual(manager.list()[0]["activeOrderId"], "order-1")
        self.assertEqual(published[0][0], "chase")

    def test_startup_recovers_unfinished_sqlite_chase_without_exchange_mutation(self):
        manager = ChaseManager(lambda *_args: None)
        recovered = manager.recover([{
            "id": "old-worker", "symbol": "PF_TESTUSD", "side": "buy", "size": 10,
            "status": "running", "activeCliOrdId": "ch-old-2", "activeOrderId": "order-2",
        }], [])
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["status"], "unknown")
        self.assertIn("SQLite", recovered[0]["events"][0])

    def test_latest_chase_snapshot_is_persisted_by_chase_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.log_event("chase", {"id": "c1", "status": "running"})
            db.log_event("chase", {"id": "c1", "status": "filled"})
            db.log_event("chase", {"id": "c2", "status": "unknown"})
            latest = {item["id"]: item for item in db.latest_chase_snapshots()}
            db._conn.close()
        self.assertEqual(latest["c1"]["status"], "filled")
        self.assertEqual(latest["c2"]["status"], "unknown")

    def test_websocket_fallback_imports_connection_type(self):
        with patch.object(market_hub.Path, "exists", return_value=False):
            open_socket, connection_type, endpoint = market_hub._import_upstream()
        self.assertTrue(callable(open_socket))
        self.assertTrue(callable(connection_type))
        self.assertTrue(callable(endpoint))

    def test_demo_account_log_uses_client_base_url(self):
        requested = []

        class Client:
            base_url = "https://demo-futures.kraken.com"

            @staticmethod
            def _auth_headers_for_path(_path, _params):
                return {}

        class Response:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            @staticmethod
            def read():
                return b'{"logs": []}'

        def open_url(req, timeout):
            requested.append((req.full_url, timeout))
            return Response()

        with patch.object(account_log.request, "urlopen", side_effect=open_url):
            account_log._get(Client(), "/api/history/v2/account-log", "count=1")
        self.assertEqual(requested, [("https://demo-futures.kraken.com/api/history/v2/account-log?count=1", 30)])

    def test_private_request_nonces_are_unique_under_concurrency(self):
        secret = base64.b64encode(b"nonce-test-secret").decode()
        client = KrakenFuturesClient(api_key="key", api_secret=secret)
        with patch("kraken_client.time.time", return_value=1000.0):
            with ThreadPoolExecutor(max_workers=16) as pool:
                nonces = list(pool.map(lambda _: client._auth_headers_for_path("/api/v3/openorders", "")["Nonce"], range(200)))
        values = [int(nonce) for nonce in nonces]
        self.assertEqual(len(set(values)), 200)
        self.assertEqual(min(values), 1_000_000)
        self.assertEqual(max(values), 1_000_199)

    def test_shared_operation_parser_requires_nested_success(self):
        confirmed = parse_operation(
            {"result": "success", "sendStatus": {"status": "placed", "order_id": "o1"}},
            "sendStatus", "placed",
        )
        rejected = parse_operation(
            {"result": "success", "sendStatus": {"status": "postWouldExecute"}},
            "sendStatus", "placed",
        )
        unknown = parse_operation({"result": "success"}, "sendStatus", "placed")
        self.assertEqual((confirmed["outcome"], confirmed["exchangeId"]), ("confirmed", "o1"))
        self.assertEqual(rejected["outcome"], "rejected")
        self.assertEqual(unknown["outcome"], "unknown")

    def test_submitted_order_gets_fresh_client_id_and_explicit_outcome(self):
        client = SimpleNamespace(post=lambda *_args, **_kwargs: {
            "result": "success", "sendStatus": {"status": "placed", "order_id": "o1"},
        })
        ctx = SimpleNamespace(
            client=client,
            instrument=lambda _symbol: {"contractValueTradePrecision": 0, "tickSize": 0.1},
            require_new_exposure=lambda _symbol: None,
            after_action=None,
        )
        result = execute_actions([{
            "type": "order", "symbol": "PF_TESTUSD", "side": "buy", "orderType": "lmt",
            "size": 10, "limitPrice": 9, "cliOrdId": "caller-reused-id",
        }], ctx, True)[0]
        self.assertEqual(result["outcome"], "confirmed")
        self.assertTrue(result["order"]["cliOrdId"].startswith("kt-order-PF_TESTUSD-"))
        self.assertNotEqual(result["order"]["cliOrdId"], "caller-reused-id")

    def test_partially_accepted_ladder_reports_partial(self):
        class Client:
            def __init__(self):
                self.index = 0

            def post(self, _path, **_kwargs):
                self.index += 1
                status = "placed" if self.index <= 2 else "postWouldExecute"
                return {"result": "success", "sendStatus": {"status": status, "order_id": f"o{self.index}"}}

        ctx = SimpleNamespace(
            client=Client(), current_price=lambda _symbol: Decimal("10"),
            instrument=lambda _symbol: {"contractValueTradePrecision": 0, "tickSize": 0.1, "contractSize": 1},
            require_new_exposure=lambda _symbol: None,
            after_action=None,
        )
        result = execute_actions([{
            "type": "ladder", "symbol": "PF_TESTUSD", "side": "buy", "notional": 300,
            "orders": 3, "depthPercent": 3, "orderType": "post",
        }], ctx, True)[0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual([row["outcome"] for row in result["responses"]], ["confirmed", "confirmed", "rejected"])
        self.assertEqual(len({row["params"]["cliOrdId"] for row in result["responses"]}), 3)

    def test_ladder_stops_after_unknown_rung(self):
        class Client:
            def __init__(self):
                self.send_calls = 0

            def post(self, path, **_kwargs):
                if path == "/orders/status":
                    return {"result": "success", "orders": []}
                self.send_calls += 1
                if self.send_calls == 2:
                    raise TimeoutError("timed out")
                return {"result": "success", "sendStatus": {"status": "placed", "order_id": "o1"}}

        client = Client()
        ctx = SimpleNamespace(
            client=client, current_price=lambda _symbol: Decimal("10"),
            instrument=lambda _symbol: {"contractValueTradePrecision": 0, "tickSize": 0.1, "contractSize": 1},
            require_new_exposure=lambda _symbol: None,
            after_action=None,
        )
        result = execute_actions([{
            "type": "ladder", "symbol": "PF_TESTUSD", "side": "buy", "notional": 400,
            "orders": 4, "depthPercent": 4, "orderType": "post",
        }], ctx, True)[0]
        self.assertEqual(client.send_calls, 2)
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(
            [row["outcome"] for row in result["responses"]],
            ["confirmed", "unknown", "rejected", "rejected"],
        )

    def test_cancel_all_with_nothing_open_is_confirmed(self):
        ctx = SimpleNamespace(client=SimpleNamespace(), get_orders=lambda: [], refresh_orders=None, after_action=None)
        result = execute_actions([{"type": "cancel_all", "symbol": "PF_TESTUSD"}], ctx, True)[0]
        self.assertEqual(result["outcome"], "confirmed")
        self.assertTrue(result["noOp"])

    def test_global_cancel_reads_and_confirms_every_current_order(self):
        calls = []
        client = SimpleNamespace(post=lambda path, params=None, **_kwargs: (
            calls.append((path, params)) or {
                "result": "success", "cancelStatus": {"status": "cancelled", "order_id": params.get("order_id")},
            }
        ))
        orders = [
            {"symbol": "PF_XBTUSD", "order_id": "x1"},
            {"symbol": "PF_SOLUSD", "order_id": "s1"},
        ]
        ctx = SimpleNamespace(client=client, get_orders=lambda: orders, refresh_orders=None, after_action=None)
        plan = build_flatten_actions("emergency", [], [{"symbol": "PF_OLDUSD", "order_id": "old"}])
        result = execute_actions(plan, ctx, True)[0]
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual([params["order_id"] for _path, params in calls], ["x1", "s1"])

    def test_global_cancel_rejects_unsupported_order_before_canceling_anything(self):
        client = Mock()
        ctx = SimpleNamespace(
            client=client,
            get_orders=lambda: [{"symbol": "FI_XBTUSD", "order_id": "x1"}],
            refresh_orders=None,
            after_action=None,
        )
        result = execute_actions([{"type": "cancel_all_orders"}], ctx, True)[0]
        self.assertEqual(result["outcome"], "rejected")
        client.post.assert_not_called()

    def test_cancel_all_reports_partial_nested_failures(self):
        orders = [
            {"symbol": "PF_TESTUSD", "cliOrdId": "c1"},
            {"symbol": "PF_TESTUSD", "cliOrdId": "c2"},
        ]
        responses = iter((
            {"result": "success", "cancelStatus": {"status": "cancelled", "order_id": "o1"}},
            {"result": "success", "cancelStatus": {"status": "notFound"}},
        ))
        client = SimpleNamespace(post=lambda *_args, **_kwargs: next(responses))
        ctx = SimpleNamespace(client=client, get_orders=lambda: orders, refresh_orders=None, after_action=None)
        result = execute_actions([{"type": "cancel_all", "symbol": "PF_TESTUSD"}], ctx, True)[0]
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual([row["outcome"] for row in result["results"]], ["confirmed", "unknown"])

    def test_unavailable_reads_preserve_last_known_state_and_age(self):
        positions = rows_payload("positions", [{
            "error": "API unavailable", "lastKnown": [{"symbol": "PF_TESTUSD"}], "ageSeconds": 4.2,
        }])
        account = account_payload({
            "error": "API unavailable", "lastKnown": {"availableMargin": 123}, "ageSeconds": 3.1,
        })
        self.assertEqual(positions, {
            "positions": [{"symbol": "PF_TESTUSD"}], "state": "unavailable",
            "error": "API unavailable", "ageSeconds": 4.2,
        })
        self.assertEqual(account["availableMargin"], 123)
        self.assertEqual((account["state"], account["ageSeconds"]), ("unavailable", 3.1))
        self.assertEqual(rows_payload("orders", []), {"orders": [], "state": "current", "ageSeconds": 0})

    def test_ambiguous_cancel_with_unavailable_orders_stays_unknown(self):
        client = SimpleNamespace(post=lambda *_args, **_kwargs: {
            "result": "success", "cancelStatus": {"status": "notFound"},
        })
        ctx = SimpleNamespace(
            client=client,
            refresh_orders=None,
            get_orders=lambda: [{"error": "open orders API unavailable"}],
        )
        result = cancel_one(ctx, {"order_id": "order-1"})
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["verification"], {"source": "openorders", "state": "unavailable"})

    def test_action_context_rejects_stale_market_or_account_state(self):
        stale = {"last": 10, "markPrice": 9.9, "time": time.time() - 30}
        ctx = ActionContext(
            client=SimpleNamespace(), hub=SimpleNamespace(ticker=lambda _symbol: stale),
            get_account=lambda: {"availableMargin": 100}, get_positions=lambda: [], get_orders=lambda: [],
            get_instruments=lambda: {"instruments": []}, get_ticker_rest=lambda _symbol: None,
        )
        with self.assertRaisesRegex(ActionError, "unavailable or stale"):
            ctx.current_price("PF_TESTUSD")
        ctx.get_ticker_rest = lambda _symbol: {"last": 10, "markPrice": 9.9, "_receivedAt": time.time()}
        self.assertEqual(ctx.current_price("PF_TESTUSD"), Decimal("10"))
        ctx.get_account = lambda: {"error": "accounts API unavailable"}
        with self.assertRaisesRegex(ActionError, "new exposure rejected"):
            ctx.require_new_exposure("PF_TESTUSD")

    def test_write_request_replay_returns_stored_result_without_reclaiming(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            try:
                payload = {"symbol": "PF_TESTUSD", "side": "buy"}
                self.assertEqual(db.claim_write_request("request-123", "/api/order", payload)["state"], "new")
                self.assertEqual(db.claim_write_request("request-123", "/api/order", payload)["state"], "pending")
                db.complete_write_request("request-123", 200, {"outcome": "confirmed", "exchangeId": "o1"})
                replay = db.claim_write_request("request-123", "/api/order", payload)
                self.assertEqual((replay["state"], replay["status"], replay["result"]["exchangeId"]), ("replay", 200, "o1"))
                self.assertEqual(
                    db.claim_write_request("request-123", "/api/order", {**payload, "side": "sell"})["state"],
                    "conflict",
                )
            finally:
                db._conn.close()

    def test_failed_write_stops_remaining_live_batch(self):
        calls = []

        def post(path, **_kwargs):
            calls.append(path)
            return {"result": "success", "sendStatus": {"status": "postWouldExecute"}}

        ctx = SimpleNamespace(
            client=SimpleNamespace(post=post),
            instrument=lambda _symbol: {"contractValueTradePrecision": 0, "tickSize": 0.1},
            require_new_exposure=lambda _symbol: None,
            after_action=None,
        )
        action = {"type": "order", "symbol": "PF_TESTUSD", "side": "buy", "orderType": "post", "size": 1, "limitPrice": 9}
        results = execute_actions([action, action], ctx, True)
        self.assertEqual(calls, ["/sendorder"])
        self.assertEqual([result["outcome"] for result in results], ["rejected", "rejected"])
        self.assertIn("not executed", results[1]["error"])

    def test_write_timeout_is_unknown_and_never_blindly_retried(self):
        calls = []

        def post(path, **_kwargs):
            calls.append(path)
            if path == "/sendorder":
                raise TimeoutError("timed out")
            return {"result": "success", "orders": []}

        result = submit_one(SimpleNamespace(client=SimpleNamespace(post=post)), {"symbol": "PF_TESTUSD", "size": 1}, "kt-test")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(calls, ["/sendorder", "/orders/status"])
        self.assertTrue(result["params"]["cliOrdId"].startswith("kt-test-"))

    def test_client_ids_are_unique(self):
        ids = {ensure_client_id({}, "kt-test")["cliOrdId"] for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_server_loads_env_before_reading_port(self):
        source = (Path(__file__).parent / "server.py").read_text(encoding="utf-8")
        self.assertLess(source.index('load_env_file(HERE / ".env")'), source.index('PORT = int(os.getenv("PORT"'))
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("PORT=9123\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                self.assertTrue(load_env_file(env_file))
                import os
                self.assertEqual(int(os.getenv("PORT", "8787")), 9123)

    def test_flatten_plans_market_closes_before_global_cancellation(self):
        positions = [
            {"symbol": "PF_XBTUSD", "side": "long", "size": 2},
            {"symbol": "PF_ETHUSD", "side": "short", "size": 3},
        ]
        orders = [
            {"symbol": "PF_XBTUSD", "order_id": "x1"},
            {"symbol": "PF_SOLUSD", "order_id": "s1"},
        ]
        emergency = build_flatten_actions("emergency", positions, orders)
        self.assertEqual([action["type"] for action in emergency], ["close", "close", "cancel_all_orders"])
        soft = build_flatten_actions("chase", positions)
        self.assertEqual(soft, [
            {"type": "chase", "symbol": "PF_XBTUSD", "side": "sell", "size": 2.0, "reduceOnly": True, "closePosition": True},
            {"type": "chase", "symbol": "PF_ETHUSD", "side": "buy", "size": 3.0, "reduceOnly": True, "closePosition": True},
        ])

    def test_symbol_soft_close_only_targets_selected_position(self):
        positions = [
            {"symbol": "PF_XBTUSD", "side": "long", "size": 1},
            {"symbol": "PF_ETHUSD", "side": "short", "size": 2},
        ]
        plan = build_flatten_actions("chase", positions, symbol="PF_ETHUSD")
        self.assertEqual(plan, [{"type": "chase", "symbol": "PF_ETHUSD", "side": "buy",
                                 "size": 2.0, "reduceOnly": True, "closePosition": True}])
        long_plan = build_flatten_actions("chase", positions, symbol="PF_XBTUSD")
        self.assertEqual([(action["symbol"], action["side"]) for action in long_plan], [("PF_XBTUSD", "sell")])
        self.assertEqual(build_flatten_actions("chase", positions, symbol="PF_SOLUSD"), [])
        with self.assertRaises(ActionError):
            build_flatten_actions("chase", [{"error": "unavailable"}], symbol="PF_ETHUSD")
        with self.assertRaises(ActionError):
            build_flatten_actions("emergency", positions, [], symbol="PF_ETHUSD")
        with self.assertRaises(ActionError):
            build_flatten_actions("chase", positions, symbol="PI_XBTUSD")

    def test_failed_emergency_close_keeps_all_orders_working(self):
        calls = []
        client = SimpleNamespace(post=lambda path, **_kwargs: (
            calls.append(path) or {"result": "success", "sendStatus": {"status": "postWouldExecute"}}
        ))
        positions = [{"symbol": "PF_XBTUSD", "side": "long", "size": 2}]
        orders = [{"symbol": "PF_XBTUSD", "cliOrdId": "protect-1"}]
        ctx = SimpleNamespace(
            client=client,
            get_positions=lambda: positions,
            get_orders=lambda: orders,
            instrument=lambda _symbol: {"contractValueTradePrecision": 0},
            after_action=None,
            refresh_orders=None,
        )
        stop_chases = Mock()
        results, _aborting = execute_emergency_flatten(
            build_flatten_actions("emergency", positions, orders), ctx, stop_chases,
        )
        self.assertEqual(calls, ["/sendorder"])
        self.assertEqual([result["outcome"] for result in results], ["rejected", "rejected"])
        self.assertIn("not executed", results[1]["error"])
        stop_chases.assert_not_called()

    def test_emergency_stops_chase_only_after_confirmed_closure_then_cancels(self):
        events = []
        positions = [{"symbol": "PF_XBTUSD", "side": "long", "size": 2}]
        orders = [{"symbol": "PF_XBTUSD", "order_id": "protect-1"}]

        def post(path, params=None, **_kwargs):
            events.append(path)
            if path == "/sendorder":
                return {"result": "success", "sendStatus": {"status": "placed", "order_id": "close-1"}}
            return {"result": "success", "cancelStatus": {"status": "cancelled", "order_id": params["order_id"]}}

        def after_action(action, _result, _armed):
            events.append(f"after:{action['type']}")
            if action["type"] == "close":
                positions.clear()

        def stop_chases():
            events.append("stop-chases")
            return {"requested": [], "completed": [], "pending": []}

        ctx = SimpleNamespace(
            client=SimpleNamespace(post=post),
            get_positions=lambda: positions,
            get_orders=lambda: orders,
            instrument=lambda _symbol: {"contractValueTradePrecision": 0},
            after_action=after_action,
            refresh_orders=None,
        )
        results, _aborting = execute_emergency_flatten(
            build_flatten_actions("emergency", positions, orders), ctx, stop_chases,
        )
        self.assertEqual([result["outcome"] for result in results], ["confirmed", "confirmed"])
        self.assertEqual(events, ["/sendorder", "after:close", "stop-chases", "/cancelorder", "after:cancel_all_orders"])

    def test_emergency_continues_when_a_planned_position_is_already_flat(self):
        positions = [{"symbol": "PF_XBTUSD", "side": "long", "size": 2}]
        orders = [{"symbol": "PF_XBTUSD", "order_id": "protect-1"}]
        plan = build_flatten_actions("emergency", positions, orders)
        positions.clear()
        client = SimpleNamespace(post=lambda _path, params=None, **_kwargs: {
            "result": "success", "cancelStatus": {"status": "cancelled", "order_id": params["order_id"]},
        })
        ctx = SimpleNamespace(
            client=client, get_positions=lambda: positions, get_orders=lambda: orders,
            after_action=None, refresh_orders=None,
        )
        results, _aborting = execute_emergency_flatten(
            plan, ctx, lambda: {"requested": [], "completed": [], "pending": []},
        )
        self.assertTrue(results[0]["noOp"])
        self.assertEqual([result["outcome"] for result in results], ["confirmed", "confirmed"])

    def test_emergency_does_not_cancel_orders_while_chase_shutdown_is_pending(self):
        client = Mock()
        orders = [{"symbol": "PF_XBTUSD", "order_id": "protect-1"}]
        ctx = SimpleNamespace(client=client, get_orders=lambda: orders, after_action=None, refresh_orders=None)
        pending = {"requested": ["chase-1"], "completed": [], "pending": [{"id": "chase-1"}]}
        results, aborting = execute_emergency_flatten(
            build_flatten_actions("emergency", [], orders), ctx, lambda: pending,
        )
        self.assertEqual(results[0]["outcome"], "unknown")
        self.assertEqual(aborting, pending)
        client.post.assert_not_called()

    def test_flatten_rejects_unavailable_state_before_dispatch(self):
        with self.assertRaisesRegex(ActionError, "position state unavailable"):
            build_flatten_actions("chase", [{"error": "positions failed"}])
        with self.assertRaisesRegex(ActionError, "order state unavailable"):
            build_flatten_actions("emergency", [], [{"error": "orders failed"}])
        with self.assertRaisesRegex(ActionError, "position side is unavailable"):
            build_flatten_actions("chase", [{"symbol": "PF_XBTUSD", "side": "?", "size": 1}])

    def test_internal_flatten_plan_can_exceed_public_batch_limit(self):
        actions = [{"type": "close", "symbol": f"PF_TEST{i}USD"} for i in range(26)]
        with self.assertRaisesRegex(ActionError, "max 25"):
            normalize_actions(actions)
        self.assertEqual(len(normalize_actions(actions, max_actions=None)), 26)

    def test_chase_simulates_disarmed_and_starts_reconciled_worker_armed(self):
        manager = Mock()
        manager.start.return_value = {"id": "chase-1", "status": "running"}
        ctx = SimpleNamespace(
            chase=manager,
            start_chase=None,
            hub=SimpleNamespace(ticker=lambda _symbol: {"bid": 1.0, "ask": 1.1}),
            get_ticker_rest=lambda _symbol: None,
            after_action=None,
        )
        action = {"type": "chase", "symbol": "PF_TESTUSD", "side": "buy", "size": 10, "reduceOnly": True}
        simulated = execute_actions([action], ctx, False)[0]
        live = execute_actions([action], ctx, True)[0]
        self.assertTrue(simulated["simulated"])
        self.assertTrue(simulated["spec"]["reduceOnly"])
        self.assertTrue(live["ok"])
        self.assertEqual(live["chase"]["id"], "chase-1")
        self.assertTrue(manager.start.call_args.args[0]["reduceOnly"])
        manager.start.assert_called_once()

    def test_closing_chase_caps_to_current_position_and_noops_when_flat(self):
        manager = Mock()
        manager.start.return_value = {"id": "close-chase", "status": "running"}
        positions = [{"symbol": "PF_TESTUSD", "side": "long", "size": 4}]
        ctx = SimpleNamespace(chase=manager, start_chase=None, get_positions=lambda: positions, after_action=None)
        action = {
            "type": "chase", "symbol": "PF_TESTUSD", "side": "sell", "size": 10,
            "reduceOnly": True, "closePosition": True,
        }
        result = execute_actions([action], ctx, True)[0]
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(manager.start.call_args.args[0]["size"], 4.0)
        positions.clear()
        result = execute_actions([action], ctx, True)[0]
        self.assertTrue(result["noOp"])
        self.assertEqual(manager.start.call_count, 1)

    def test_missing_action_type_is_repaired_for_order_payload(self):
        raw = [{"symbol": "PF_ETHUSD", "side": "buy", "orderType": "post", "size": 1, "limitPrice": 2000}]
        normalized = normalize_actions(raw)
        self.assertEqual(normalized[0]["type"], "order")
        self.assertNotIn("type", raw[0])

    def test_malformed_batch_is_rejected_before_dispatch(self):
        with self.assertRaisesRegex(ActionError, "action 2"):
            normalize_actions([
                {"type": "order", "symbol": "PF_ETHUSD", "side": "buy", "orderType": "post", "size": 1, "limitPrice": 2000},
                {"type": ""},
            ])

    @staticmethod
    def _open_tp(**changes):
        order = {
            "symbol": "PF_TESTUSD", "side": "sell", "orderType": "take_profit",
            "reduceOnly": True, "order_id": "tp-1", "cliOrdId": f"{MANAGED_TP_PREFIX}PF_TESTUSD-old",
            "size": 10, "unfilledSize": 10, "stopPrice": 120, "triggerSignal": "mark",
        }
        order.update(changes)
        return order

    def test_protection_edit_uses_exact_id_and_preserves_exchange_id(self):
        client = ProtectionScriptClient({
            "/editorder": [{"result": "success", "editStatus": {"status": "edited", "orderId": "tp-1"}}],
        })
        ctx = ProtectionExecutionContext(client, [self._open_tp()])
        result = execute_actions([{
            "type": "replace_tp", "symbol": "PF_TESTUSD", "stopPrice": 125, "orderId": "tp-1",
        }], ctx, True)[0]
        self.assertTrue(result["ok"])
        self.assertTrue(result["edited"])
        self.assertEqual(result["orderId"], "tp-1")
        self.assertEqual(client.calls, [("/editorder", {"orderId": "tp-1", "size": 10.0, "stopPrice": 125.0})])

    def test_protection_edit_rejection_leaves_original_order_working(self):
        client = ProtectionScriptClient({
            "/editorder": [{"result": "success", "editStatus": {"status": "invalidSize"}}],
        })
        ctx = ProtectionExecutionContext(client, [self._open_tp()])
        result = execute_actions([{
            "type": "replace_tp", "symbol": "PF_TESTUSD", "stopPrice": 125, "orderId": "tp-1",
        }], ctx, True)[0]
        self.assertFalse(result["ok"])
        self.assertTrue(result["originalWorking"])
        self.assertEqual([path for path, _params in client.calls], ["/editorder"])
        self.assertEqual(ctx.alerts, [])

    def test_edit_not_found_without_original_order_sets_unprotected_alert(self):
        client = ProtectionScriptClient({
            "/editorder": [{"result": "success", "editStatus": {"status": "orderForEditNotFound"}}],
        })
        ctx = ProtectionExecutionContext(client, [self._open_tp()])
        reads = iter(([self._open_tp()], []))
        ctx.get_orders = lambda: next(reads)
        result = execute_actions([{
            "type": "replace_tp", "symbol": "PF_TESTUSD", "stopPrice": 125, "orderId": "tp-1",
        }], ctx, True)[0]
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(ctx.alerts[0][0:2], ("PF_TESTUSD", "TP"))

    def test_exact_partial_tp_drag_preserves_size_and_other_ladder_orders(self):
        partial = self._open_tp(unfilledSize=4, size=4)
        other = self._open_tp(order_id="tp-2", cliOrdId="manual-tp-2", unfilledSize=6, size=6, stopPrice=130)
        ctx = ProtectionExecutionContext(Mock(), [partial, other])
        result = execute_actions([{
            "type": "replace_tp", "symbol": "PF_TESTUSD", "stopPrice": 126,
            "orderId": "tp-1", "preserveSize": True,
        }], ctx, False)[0]
        self.assertTrue(result["simulated"])
        self.assertEqual(result["editParams"], {"orderId": "tp-1", "size": 4.0, "stopPrice": 126.0})
        ambiguous = execute_actions([{
            "type": "replace_tp", "symbol": "PF_TESTUSD", "stopPrice": 126,
        }], ctx, False)[0]
        self.assertFalse(ambiguous["ok"])
        self.assertIn("ladder", ambiguous["error"])

    def test_cancel_recreate_rolls_back_previous_protection_on_known_rejection(self):
        target = self._open_tp(order_id=None)
        client = ProtectionScriptClient({
            "/cancelorder": [{"result": "success", "cancelStatus": {"status": "cancelled"}}],
            "/sendorder": [
                {"result": "success", "sendStatus": {"status": "insufficientAvailableFunds"}},
                {"result": "success", "sendStatus": {"status": "placed", "order_id": "rollback-1"}},
            ],
        })
        ctx = ProtectionExecutionContext(client, [target])
        result = execute_actions([{
            "type": "replace_tp", "symbol": "PF_TESTUSD", "stopPrice": 125, "cliOrdId": target["cliOrdId"],
        }], ctx, True)[0]
        self.assertFalse(result["ok"])
        self.assertTrue(result["rolledBack"])
        self.assertEqual([path for path, _params in client.calls], ["/cancelorder", "/sendorder", "/sendorder"])
        self.assertEqual(ctx.alerts, [])

    def test_cancel_timeout_with_original_still_open_never_sends_replacement(self):
        target = self._open_tp(order_id=None)
        client = ProtectionScriptClient({"/cancelorder": [TimeoutError("timed out")]})
        ctx = ProtectionExecutionContext(client, [target])
        result = execute_actions([{
            "type": "replace_tp", "symbol": "PF_TESTUSD", "stopPrice": 125, "cliOrdId": target["cliOrdId"],
        }], ctx, True)[0]
        self.assertFalse(result["ok"])
        self.assertTrue(result["originalWorking"])
        self.assertEqual([path for path, _params in client.calls], ["/cancelorder"])

    def test_unknown_replacement_is_reconciled_before_any_rollback(self):
        target = self._open_tp(order_id=None)
        client = ProtectionScriptClient({
            "/cancelorder": [{"result": "success", "cancelStatus": {"status": "cancelled"}}],
            "/sendorder": [TimeoutError("timed out")],
            "/orders/status": [{"result": "success", "orders": []}],
        })
        ctx = ProtectionExecutionContext(client, [target])
        result = execute_actions([{
            "type": "replace_tp", "symbol": "PF_TESTUSD", "stopPrice": 125, "cliOrdId": target["cliOrdId"],
        }], ctx, True)[0]
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual([path for path, _params in client.calls], ["/cancelorder", "/sendorder", "/orders/status"])
        self.assertEqual(ctx.alerts[0][0:2], ("PF_TESTUSD", "TP"))

    def test_rollback_failure_persists_unprotected_alert(self):
        target = self._open_tp(order_id=None)
        rejected = {"result": "success", "sendStatus": {"status": "insufficientAvailableFunds"}}
        client = ProtectionScriptClient({
            "/cancelorder": [{"result": "success", "cancelStatus": {"status": "cancelled"}}],
            "/sendorder": [rejected, rejected],
        })
        ctx = ProtectionExecutionContext(client, [target])
        result = execute_actions([{
            "type": "replace_tp", "symbol": "PF_TESTUSD", "stopPrice": 125, "cliOrdId": target["cliOrdId"],
        }], ctx, True)[0]
        self.assertFalse(result["ok"])
        self.assertTrue(result["unprotected"])
        self.assertEqual(ctx.alerts[0][0:2], ("PF_TESTUSD", "TP"))

    def test_unprotected_alert_is_persistent_until_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            db.set_protection_alert("PF_TESTUSD", "TP", {"message": "rollback failed"})
            alerts = db.protection_alerts()
            db.clear_protection_alert("PF_TESTUSD", "TP")
            cleared = db.protection_alerts()
            db._conn.close()
        self.assertEqual(alerts[0]["status"], "UNPROTECTED")
        self.assertEqual(alerts[0]["details"]["message"], "rollback failed")
        self.assertEqual(cleared, [])

    def test_tp_and_sl_replace_only_the_same_protection_type(self):
        ctx = ProtectionContext()
        tp = _replace_protection_plan({"symbol": "PF_EGLDUSD", "stopPrice": 4.622}, ctx, "take_profit")
        sl = _replace_protection_plan({"symbol": "PF_EGLDUSD", "stopPrice": 4.715}, ctx, "stp")
        self.assertEqual(tp["orderId"], "tp-1")
        self.assertEqual(sl["orderId"], "sl-1")
        self.assertEqual(tp["editParams"], {"orderId": "tp-1", "size": 644.41, "stopPrice": 4.622})
        self.assertEqual(sl["editParams"], {"orderId": "sl-1", "size": 644.41, "stopPrice": 4.715})
        self.assertEqual(tp["order"]["orderType"], "take_profit")
        self.assertEqual(sl["order"]["orderType"], "stp")

    def test_managed_full_tp_resizes_after_position_growth(self):
        positions = [{"symbol": "PF_ENAUSD", "side": "long", "size": 20626}]
        managed = {
            "symbol": "PF_ENAUSD", "orderType": "take_profit", "reduceOnly": True,
            "cliOrdId": f"{MANAGED_TP_PREFIX}PF_ENAUSD-1", "order_id": "managed-tp",
            "unfilledSize": 10313, "stopPrice": 0.17296,
        }
        actions = managed_protection_sync_actions(positions, [managed])
        self.assertEqual(actions, [{
            "type": "replace_tp", "symbol": "PF_ENAUSD", "stopPrice": 0.17296,
            "managed": True, "syncFromSize": 10313.0, "syncToSize": 20626.0,
            "orderId": "managed-tp", "sourceCliOrdId": f"{MANAGED_TP_PREFIX}PF_ENAUSD-1",
        }])
        self.assertEqual(managed_protection_sync_actions(positions, [{**managed, "unfilledSize": 20626}]), [])
        legacy = {**managed, "cliOrdId": "", "order_id": "legacy-tp"}
        self.assertEqual(len(managed_protection_sync_actions(positions, [legacy], {"legacy-tp"})), 1)
        self.assertEqual(managed_protection_sync_actions(positions, [{**managed, "cliOrdId": "manual-tp"}]), [])

    def test_managed_tp_does_not_rewrite_partial_ladders(self):
        positions = [{"symbol": "PF_ENAUSD", "side": "long", "size": 20626}]
        managed = {
            "symbol": "PF_ENAUSD", "orderType": "take_profit", "reduceOnly": True,
            "cliOrdId": f"{MANAGED_TP_PREFIX}PF_ENAUSD-1", "unfilledSize": 10313,
            "stopPrice": 0.17296,
        }
        partial = {**managed, "cliOrdId": "manual-partial", "unfilledSize": 2000, "stopPrice": 0.18}
        self.assertEqual(managed_protection_sync_actions(positions, [managed, partial]), [])

    def test_protection_price_must_match_mark_trigger_direction(self):
        ctx = ProtectionContext()
        with self.assertRaisesRegex(ActionError, "above current mark"):
            _replace_protection_plan({"symbol": "PF_EGLDUSD", "stopPrice": 4.64}, ctx, "stp")
        with self.assertRaisesRegex(ActionError, "below current mark"):
            _replace_protection_plan({"symbol": "PF_EGLDUSD", "stopPrice": 4.66}, ctx, "take_profit")

    def test_profitable_trailing_stop_is_valid(self):
        plan = _replace_protection_plan(
            {"symbol": "PF_TRUMPUSD", "stopPrice": 2.19}, LongProtectionContext(), "stp"
        )
        self.assertEqual(plan["order"]["stopPrice"], 2.19)

    def test_replace_sl_simulates_without_client_call(self):
        result = execute_actions([{"type": "replace_sl", "symbol": "PF_EGLDUSD", "stopPrice": 4.715}], ProtectionContext(), False)[0]
        self.assertTrue(result["simulated"])
        self.assertEqual(result["order"]["orderType"], "stp")

    def test_close_then_protect_uses_refreshed_remaining_size(self):
        results = execute_actions([
            {"type": "close", "symbol": "PF_TRUMPUSD", "percent": 40},
            {"type": "replace_sl", "symbol": "PF_TRUMPUSD", "stopPrice": 2.19},
        ], SequentialProtectionContext(), True)
        self.assertEqual(results[0]["remainingSize"], 1482.8)
        self.assertEqual(results[1]["order"]["size"], 1482.8)

    def test_protection_fails_closed_when_open_orders_cannot_be_read(self):
        ctx = LongProtectionContext()
        ctx.get_orders = lambda: [{"error": "private API unavailable"}]
        result = execute_actions([
            {"type": "replace_sl", "symbol": "PF_TRUMPUSD", "stopPrice": 2.19},
        ], ctx, False)[0]
        self.assertFalse(result["ok"])
        self.assertIn("cannot read open orders", result["error"])

    def test_unexpected_state_refresh_failure_stops_remaining_batch(self):
        ctx = LongProtectionContext()
        def fail_refresh(*_args):
            raise OSError("offline")
        ctx.after_action = fail_refresh
        results = execute_actions([
            {"type": "close", "symbol": "PF_TRUMPUSD", "percent": 40},
            {"type": "replace_sl", "symbol": "PF_TRUMPUSD", "stopPrice": 2.19},
        ], ctx, False)
        self.assertIn("state refresh failed", results[0]["stateRefreshError"])
        self.assertIn("not executed", results[1]["error"])

    def test_snapshot_contains_complete_protection_state(self):
        snapshot = ai_chat.build_context_snapshot(
            account={}, positions=[], symbol="PF_TRUMPUSD", ticker=None, candles=[],
            orders=[{
                "order_id": "sl-1", "symbol": "PF_TRUMPUSD", "side": "sell",
                "orderType": "stop", "stopPrice": 2.19, "filledSize": 0,
                "unfilledSize": 1482.8, "reduceOnly": True, "triggerSignal": "mark",
            }],
        )
        self.assertIn('"orderId": "sl-1"', snapshot)
        self.assertIn('"size": 1482.8', snapshot)
        self.assertIn('"stopPrice": 2.19', snapshot)
        self.assertIn('"triggerSignal": "mark"', snapshot)

    def test_live_snapshot_follows_and_overrides_memory(self):
        seen = []
        def completion(_key, _model, messages):
            seen.append(messages[0]["content"])
            return {"choices": [{"message": {"content": "done"}}]}
        with patch("ai_chat._load_openrouter_key", return_value="key"), patch("ai_chat._openrouter_completion", side_effect=completion):
            ai_chat.respond(
                [{"role": "user", "content": "status"}], "OPEN POSITIONS: live",
                session_memory="Open plan: old", session_summary="older context",
            )
        self.assertLess(seen[0].index("DURABLE MEMORY"), seen[0].index("LIVE SNAPSHOT (AUTHORITATIVE"))
        self.assertTrue(seen[0].endswith("OPEN POSITIONS: live"))

    def test_compaction_excludes_proposals_and_execution_traces(self):
        messages = [
            {"role": "user", "content": "prefer maker"},
            {"role": "assistant", "content": "planned", "meta": {"actionProposals": [[{"type": "close"}]]}},
            {"role": "assistant", "content": "failed", "meta": {"trace": {"working": False}}},
        ]
        self.assertEqual(ai_chat._compaction_messages(messages), [messages[0]])

    def test_failed_compaction_summary_keeps_source_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            try:
                session_id = db.ensure_session()["id"]
                for index in range(5):
                    db.add_message(session_id, "user", f"message {index} " + "x" * 1000)
                with self.assertRaisesRegex(ai_chat.ChatError, "summary failed"):
                    chat_compaction.prepare_chat_context(
                        db, session_id, "snapshot", 1, keep_last=2,
                        summarizer=lambda *_args, **_kwargs: (_ for _ in ()).throw(ai_chat.ChatError("summary failed")),
                    )
                self.assertEqual(db.count_in_context(session_id), 5)
                self.assertEqual(db.get_session_summary(session_id), "")
            finally:
                db._conn.close()

    def test_compaction_commit_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            try:
                session_id = db.ensure_session()["id"]
                for index in range(5):
                    db.add_message(session_id, "user", f"message {index}")
                candidates, old_summary = db.compaction_candidates(session_id, keep_last=2)
                self.assertEqual((len(candidates), db.count_in_context(session_id)), (3, 5))
                db._conn.execute(
                    "CREATE TRIGGER fail_compaction BEFORE UPDATE OF summary ON sessions "
                    "BEGIN SELECT RAISE(ABORT, 'forced'); END"
                )
                db._conn.commit()
                with self.assertRaises(sqlite3.DatabaseError):
                    db.commit_compaction(session_id, [row["id"] for row in candidates], old_summary, "summary")
                self.assertEqual(db.count_in_context(session_id), 5)
                self.assertEqual(db.get_session_summary(session_id), "")
            finally:
                db._conn.close()

    def test_compaction_runs_before_pending_request(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            try:
                session_id = db.ensure_session()["id"]
                for index in range(5):
                    db.add_message(session_id, "user", f"message {index} " + "x" * 9000)
                history = [{"role": row["role"], "content": row["content"]} for row in db.get_messages(session_id, 400)]
                before = ai_chat.estimate_prompt_tokens(history, "snapshot")
                projected = ai_chat.estimate_prompt_tokens(history[-2:], "snapshot", session_summary="short summary")
                events = []

                def summarize(_messages, _summary, usage_sink):
                    events.append("summarize")
                    usage_sink({"total_tokens": 7})
                    return "short summary"

                prepared = chat_compaction.prepare_chat_context(
                    db, session_id, "snapshot", (before + projected) // 2,
                    keep_last=2, summarizer=summarize,
                )
                events.append("request")
                self.assertEqual(events, ["summarize", "request"])
                self.assertEqual((prepared[4], db.count_in_context(session_id)), (3, 2))
                self.assertEqual(db.get_token_usage(session_id)["billed_tokens"], 7)
            finally:
                db._conn.close()

    def test_tool_round_usage_keeps_latest_prompt_separate_from_billing(self):
        responses = [
            {"usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}, "choices": [{"message": {"content": "", "tool_calls": [{"id": "one", "function": {"name": "get_positions", "arguments": "{}"}}]}}]},
            {"usage": {"prompt_tokens": 140, "completion_tokens": 20, "total_tokens": 160}, "choices": [{"message": {"content": "done"}}]},
        ]
        with patch("ai_chat._load_openrouter_key", return_value="key"), patch("ai_chat._openrouter_completion", side_effect=responses):
            result = ai_chat.respond(
                [{"role": "user", "content": "status"}], "snapshot",
                tool_executor=lambda *_args: {"positions": []},
            )
        self.assertEqual(result["usage"], {
            "prompt_tokens": 140,
            "billed_prompt_tokens": 240,
            "completion_tokens": 30,
            "total_tokens": 270,
        })

    def test_model_usage_persists_latest_prompt_and_cumulative_billing(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            try:
                session_id = db.ensure_session()["id"]
                db.record_model_usage(session_id, 140, 270)
                db.record_model_usage(session_id, 90, 120)
                self.assertEqual(db.get_token_usage(session_id), {"prompt_tokens": 90, "billed_tokens": 390})
            finally:
                db._conn.close()

    def test_tool_audit_and_volatile_memory_rejection(self):
        calls = [{
            "id": "memory", "function": {
                "name": "update_memory",
                "arguments": json.dumps({"memory": "Open Positions:\n- PF_TRUMPUSD long"}),
            },
        }, {
            "id": "positions", "function": {"name": "get_positions", "arguments": "{}"},
        }]
        responses = [
            {"choices": [{"message": {"content": "", "tool_calls": calls}}]},
            {"choices": [{"message": {"content": "done"}}]},
        ]
        audit = []
        with patch("ai_chat._load_openrouter_key", return_value="key"), patch("ai_chat._openrouter_completion", side_effect=responses):
            result = ai_chat.respond(
                [{"role": "user", "content": "status"}], "snapshot",
                tool_executor=lambda name, _args: {"positions": []} if name == "get_positions" else {},
                tool_audit=audit.append,
            )
        self.assertIsNone(result["memory"])
        self.assertEqual([row["name"] for row in audit], ["update_memory", "get_positions"])
        self.assertEqual([row["callId"] for row in audit], ["memory", "positions"])
        self.assertIn("rejected", audit[0]["result"])

    def test_direct_ladder_uses_documented_depth_percent(self):
        result = execute_actions([{
            "type": "ladder", "symbol": "PF_TRUMPUSD", "side": "buy",
            "notional": 240, "orders": 2, "depthPercent": 1,
        }], LongProtectionContext(), False)[0]
        self.assertTrue(result["simulated"])
        self.assertEqual(len(result["orders"]), 2)

    def test_future_read_tools_are_registered(self):
        tools = {tool["function"]["name"]: tool["function"] for tool in ai_chat.TOOLS}
        self.assertTrue({"get_chases", "get_trade_history", "scan_markets"} <= tools.keys())
        self.assertIn("explicitly asks for a draft", tools["propose_actions"]["description"])
        self.assertIn("matching direct write tool", ai_chat.SYSTEM_PROMPT)
        ladder = tools["place_ladder"]["parameters"]
        self.assertEqual(ladder["required"], ["symbol", "side", "orders"])
        self.assertTrue({"depthPercent", "startPrice", "endPrice", "size", "notional", "reduceOnly"} <= ladder["properties"].keys())
        self.assertNotIn("rangePercent", ladder["properties"])

    def test_market_scan_is_pf_only_and_parameter_cached(self):
        scanner._vol_cache.clear()
        client = ScannerClient()
        first = scanner.scan_volatility(client, window_minutes=5, limit=10, min_volume_quote=0, max_spread_percent=1)
        second = scanner.scan_volatility(client, window_minutes=5, limit=10, min_volume_quote=0, max_spread_percent=1)
        scanner.scan_volatility(client, window_minutes=5, limit=5, min_volume_quote=0, max_spread_percent=1)
        self.assertEqual([row["symbol"] for row in first["rows"]], ["PF_TESTUSD"])
        self.assertTrue(second["cached"])
        self.assertEqual(client.ticker_calls, 2)

    @patch("account_log._get", side_effect=OSError("gateway down"))
    def test_account_log_pagination_failure_is_explicit_and_preserves_cache(self, _get):
        old_cache, old_ts = account_log._page_cache, account_log._cache_ts
        account_log._page_cache, account_log._cache_ts = [{"id": 7}], 0
        try:
            with self.assertRaisesRegex(RuntimeError, "pagination failed"):
                account_log.full_log(None, force=True)
            self.assertEqual(account_log._page_cache, [{"id": 7}])
        finally:
            account_log._page_cache, account_log._cache_ts = old_cache, old_ts

    @patch("account_log.full_log")
    def test_trade_history_combines_execution_rows(self, full_log):
        full_log.return_value = [
            {"id": 1, "date": "2026-09-03T12:00:00.000Z", "info": "futures trade", "contract": "pf_trumpusd", "execution": "exec-1", "asset": "pf_trumpusd", "old_balance": 200, "new_balance": 100, "trade_price": 2.4, "mark_price": 2.4},
            {"id": 2, "date": "2026-09-03T12:00:00.000Z", "info": "futures trade", "contract": "pf_trumpusd", "execution": "exec-1", "asset": "usd", "realized_pnl": 10, "realized_funding": 0.2, "fee": 0.5, "trade_price": 2.4, "mark_price": 2.4},
        ]
        row = account_log.trade_history(None, "PF_TRUMPUSD", 10)[0]
        self.assertEqual((row["side"], row["size"]), ("sell", 100.0))
        self.assertAlmostEqual(row["netPnl"], 9.7)

    def test_legacy_managed_protection_is_inferred_from_live_action_log(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            try:
                db.log_action("api", True,
                    [{"type": "replace_tp", "symbol": "PF_ENAUSD", "stopPrice": 0.17}],
                    [{"ok": True, "response": {"sendStatus": {"status": "placed", "order_id": "tp-live"}}}],
                )
                db.log_action("api", False,
                    [{"type": "replace_tp", "symbol": "PF_ENAUSD", "stopPrice": 0.18}],
                    [{"ok": True, "simulated": True, "response": {"sendStatus": {"order_id": "tp-sim"}}}],
                )
                self.assertEqual(db.inferred_managed_protection_ids(), {"tp-live"})
            finally:
                db._conn.close()

    def test_proposal_claim_is_at_most_once(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            try:
                session_id = db.ensure_session()["id"]
                block = [{"type": "order", "symbol": "PF_ETHUSD", "side": "buy", "orderType": "post", "size": 1, "limitPrice": 2000}]
                message_id = db.add_message(session_id, "assistant", "proposal", {"actionProposals": [block]})
                self.assertTrue(db.claim_action_proposal(session_id, message_id, 0, block))
                self.assertFalse(db.claim_action_proposal(session_id, message_id, 0, block))
            finally:
                db._conn.close()

    def test_local_write_security_rejects_bad_headers(self):
        security = LocalSecurity(8787)
        valid = {
            "Host": "127.0.0.1:8787",
            "Origin": "http://127.0.0.1:8787",
            "Content-Type": "application/json; charset=utf-8",
            "X-Terminal-Token": security.token,
        }
        self.assertIsNone(security.validate_write(valid))
        for key, value, status in (
            ("Host", "evil.test", 403),
            ("Origin", "https://evil.test", 403),
            ("Content-Type", "text/plain", 415),
            ("X-Terminal-Token", "wrong", 403),
        ):
            headers = {**valid, key: value}
            self.assertEqual(security.validate_write(headers)[0], status)

    def test_arm_challenge_is_tied_to_process_token_and_one_time(self):
        security = LocalSecurity(8787)
        challenge = security.issue_arm_challenge()
        self.assertTrue(security.consume_arm_challenge(challenge))
        self.assertFalse(security.consume_arm_challenge(challenge))
        other_process = LocalSecurity(8787)
        self.assertFalse(other_process.consume_arm_challenge(challenge))
        expired = security.issue_arm_challenge(ttl_seconds=-1)
        self.assertFalse(security.consume_arm_challenge(expired))

    def test_static_path_must_remain_inside_root(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "static"
            root.mkdir()
            allowed = root / "index.html"
            allowed.write_text("ok")
            (parent / "secret.txt").write_text("no")
            self.assertEqual(safe_static_path(root, "/index.html"), allowed.resolve())
            self.assertIsNone(safe_static_path(root, "/../secret.txt"))

    @patch("ai_chat.time.sleep", return_value=None)
    @patch("ai_chat.requests.post")
    def test_json_level_504_is_retried(self, post, _sleep):
        post.side_effect = [
            FakeResponse({"error": {"message": "The operation was aborted", "code": 504}}),
            FakeResponse({"choices": [{"message": {"content": "ok"}}]}),
        ]
        data = ai_chat._openrouter_completion("key", "model", [{"role": "user", "content": "hi"}])
        self.assertEqual(data["choices"][0]["message"]["content"], "ok")
        self.assertEqual(post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
