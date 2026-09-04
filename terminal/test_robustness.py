import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import account_log
import ai_chat
import scanner
from actions import (
    MANAGED_TP_PREFIX, ActionError, _replace_protection_plan,
    execute_actions, managed_protection_sync_actions, normalize_actions,
)
from db import Database


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
            {"symbol": "PF_EGLDUSD", "orderType": "take_profit", "reduceOnly": True, "order_id": "tp-1"},
            {"symbol": "PF_EGLDUSD", "orderType": "stop", "reduceOnly": True, "order_id": "sl-1"},
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
    def post(self, _path, **_kwargs):
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
    def test_live_chase_is_disabled_while_simulation_remains_available(self):
        manager = Mock()
        ctx = SimpleNamespace(
            chase=manager,
            hub=SimpleNamespace(ticker=lambda _symbol: {"bid": 1.0, "ask": 1.1}),
            get_ticker_rest=lambda _symbol: None,
            after_action=None,
        )
        action = {"type": "chase", "symbol": "PF_TESTUSD", "side": "buy", "size": 10}
        simulated = execute_actions([action], ctx, False)[0]
        rejected = execute_actions([action], ctx, True)[0]
        self.assertTrue(simulated["simulated"])
        self.assertFalse(rejected["ok"])
        self.assertIn("temporarily disabled", rejected["error"])
        manager.start.assert_not_called()

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

    def test_tp_and_sl_replace_only_the_same_protection_type(self):
        ctx = ProtectionContext()
        tp = _replace_protection_plan({"symbol": "PF_EGLDUSD", "stopPrice": 4.622}, ctx, "take_profit")
        sl = _replace_protection_plan({"symbol": "PF_EGLDUSD", "stopPrice": 4.715}, ctx, "stp")
        self.assertEqual(tp["cancelOrderIds"], [{"order_id": "tp-1"}])
        self.assertEqual(sl["cancelOrderIds"], [{"order_id": "sl-1"}])
        self.assertEqual(tp["order"]["orderType"], "take_profit")
        self.assertEqual(sl["order"]["orderType"], "stp")
        self.assertTrue(tp["order"]["cliOrdId"].startswith(MANAGED_TP_PREFIX))

    def test_managed_full_tp_resizes_after_position_growth(self):
        positions = [{"symbol": "PF_ENAUSD", "side": "long", "size": 20626}]
        managed = {
            "symbol": "PF_ENAUSD", "orderType": "take_profit", "reduceOnly": True,
            "cliOrdId": f"{MANAGED_TP_PREFIX}PF_ENAUSD-1", "unfilledSize": 10313,
            "stopPrice": 0.17296,
        }
        actions = managed_protection_sync_actions(positions, [managed])
        self.assertEqual(actions, [{
            "type": "replace_tp", "symbol": "PF_ENAUSD", "stopPrice": 0.17296,
            "managed": True, "syncFromSize": 10313.0, "syncToSize": 20626.0,
            "sourceCliOrdId": f"{MANAGED_TP_PREFIX}PF_ENAUSD-1",
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
        self.assertIn("depthPercent", ladder["required"])
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
