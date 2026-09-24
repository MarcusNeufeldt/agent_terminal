"""Signed-action tests. Transport is always injected; nothing here touches the network."""

import ast
import json
import threading
import time
from decimal import Decimal
import unittest
from pathlib import Path
from unittest.mock import Mock

from hyperliquid_client import HyperliquidError
from hyperliquid_signing import address_from_private_key, private_key_from_hex
from hyperliquid.utils.signing import recover_agent_or_user_from_l1_action
from hyperliquid_trading import (ALLOWED_ACTIONS, ExchangeTransport, HyperliquidRejectedError, HyperliquidTrader,
                                 NonceSource, TradingDisabledError, build_order, build_trader,
                                 check_signer_identity, classify, client_order_id, format_price, format_size,
                                 load_trading_credentials, parse_exchange_response)

FOUNDRY_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ACCOUNT = "0x" + "ab" * 20
SIGNER = address_from_private_key(private_key_from_hex(FOUNDRY_KEY))
MARKETS = {"HL_APT": {"symbol": "HL_APT", "coin": "APT", "assetId": 1, "contractValueTradePrecision": 2,
                      "maxLeverage": 10},
           "HL_BTC": {"symbol": "HL_BTC", "coin": "BTC", "assetId": 0, "contractValueTradePrecision": 5,
                      "maxLeverage": 40}}


class RecordingTransport:
    def __init__(self, response=None, error=None):
        self.payloads = []
        self.response = response if response is not None else {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 77738308}}]}}}
        self.error = error

    def post(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error
        return self.response


def make_trader(response=None, error=None, network="testnet", nonce=None):
    return HyperliquidTrader(MARKETS.get, transport=RecordingTransport(response, error),
                             private_key=FOUNDRY_KEY, account_address=ACCOUNT, network=network,
                             nonce=nonce or NonceSource())


class PrecisionTests(unittest.TestCase):
    def test_price_significant_figure_examples_from_the_docs(self):
        self.assertEqual(format_price(1234.5, 0), "1234.5")
        self.assertEqual(format_price(1234.56, 0), "1234.6")      # 5 significant figures
        self.assertEqual(format_price(0.001234, 0), "0.001234")   # 6 decimals allowed
        self.assertEqual(format_price(0.0012345, 0), "0.001235")  # capped at 6 decimals
        self.assertEqual(format_price(0.0012345, 1), "0.00123")   # capped at 6 - szDecimals
        self.assertEqual(format_price(12345.6, 0), "12346")       # integer only above 10^4
        self.assertEqual(format_price(123456.7, 5), "123457")     # integer prices always allowed
        self.assertEqual(format_price(0.63, 2), "0.63")

    def test_price_output_has_no_trailing_zeros_or_exponent_notation(self):
        for value, decimals in ((1, 0), (2.0, 2), (1000, 5), (0.5, 1), (123456, 0)):
            text = format_price(value, decimals)
            self.assertNotIn("E", text.upper())
            if "." in text:
                self.assertFalse(text.endswith("0"), f"{text} keeps a trailing zero")
            self.assertEqual(format_price(text, decimals), text)

    def test_price_rejects_non_positive_and_invalid(self):
        for bad in (0, -1, "NaN", "Infinity", None, "abc"):
            with self.subTest(bad=bad), self.assertRaises(HyperliquidError):
                format_price(bad, 0)

    def test_size_rounds_down_so_a_request_never_exceeds_intent(self):
        self.assertEqual(format_size(2.999, 2), "2.99")
        self.assertEqual(format_size(2.5, 0), "2")
        self.assertEqual(format_size(0.0001, 5), "0.0001")
        self.assertEqual(format_size(12, 0), "12")

    def test_size_rejects_values_that_collapse_to_zero(self):
        with self.assertRaises(HyperliquidError):
            format_size(0.004, 2)
        with self.assertRaises(HyperliquidError):
            format_size(10, 7)
        for bad in (0, -5, "NaN", None):
            with self.subTest(bad=bad), self.assertRaises(HyperliquidError):
                format_size(bad, 2)


class WireFormatTests(unittest.TestCase):
    def test_order_keys_and_short_names_match_the_documented_schema(self):
        order = build_order(1, "buy", "2.5", "0.63", "alo", False, cloid="0x" + "1" * 32)
        self.assertEqual(list(order), ["type", "orders", "grouping"])
        self.assertEqual(list(order["orders"][0]), ["a", "b", "p", "s", "r", "t", "c"])
        self.assertEqual(order["orders"][0], {"a": 1, "b": True, "p": "0.63", "s": "2.5", "r": False,
                                             "t": {"limit": {"tif": "Alo"}}, "c": "0x" + "1" * 32})

    def test_sell_and_reduce_only_flags(self):
        order = build_order(0, "sell", "1", "100", "ioc", True)["orders"][0]
        self.assertEqual((order["b"], order["r"], order["t"]), (False, True, {"limit": {"tif": "Ioc"}}))

    def test_trigger_order_shape(self):
        order = build_order(1, "sell", "1", "0.7", "gtc", True,
                            trigger={"kind": "tp", "triggerPx": "0.7", "market": False})["orders"][0]
        self.assertEqual(order["t"], {"trigger": {"isMarket": False, "triggerPx": "0.7", "tpsl": "tp"}})

    def test_invalid_targets_are_refused(self):
        cases = [dict(side="long"), dict(tif="fok"), dict(reduce_only="false"),
                 dict(trigger={"kind": "stop", "triggerPx": "1", "market": False}),
                 dict(trigger={"kind": "tp", "triggerPx": "1", "market": "no"}),
                 dict(cloid="0x1234"), dict(grouping="bracket")]
        for changes in cases:
            base = dict(action_asset=1, side="buy", size="1", price="1", tif="gtc", reduce_only=False)
            base.update(changes)
            with self.subTest(changes=changes), self.assertRaises(HyperliquidError):
                build_order(**base)

    def test_client_order_ids_are_unique_and_correctly_sized(self):
        ids = {client_order_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for value in ids:
            self.assertRegex(value, r"^0x[0-9a-f]{32}$")


class ResponseTests(unittest.TestCase):
    def test_resting_and_filled_statuses(self):
        resting = parse_exchange_response({"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"resting": {"oid": 77738308}}]}}})
        self.assertEqual(resting["rows"], [{"state": "resting", "oid": 77738308}])
        filled = parse_exchange_response({"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"filled": {"totalSz": "0.02", "avgPx": "1891.4", "oid": 123}}]}}})
        self.assertEqual(filled["rows"], [{"state": "filled", "oid": 123, "totalSize": "0.02",
                                           "averagePrice": "1891.4"}])

    def test_error_status_keeps_the_exchange_message(self):
        parsed = parse_exchange_response({"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"error": "Order must have minimum value of $10."}]}}})
        self.assertEqual(parsed["rows"][0]["error"], "Order must have minimum value of $10.")

    def test_top_level_error_and_default_responses(self):
        with self.assertRaisesRegex(HyperliquidError, "does not exist"):
            parse_exchange_response({"status": "err", "response": "L1 error: User or API Wallet 0x1 does not exist."})
        default = parse_exchange_response({"status": "ok", "response": {"type": "default"}})
        self.assertEqual(default["rows"], [])

    def test_malformed_responses_are_rejected(self):
        for payload in ([], None, {"status": "ok"}, {"status": "ok", "response": []},
                        {"status": "ok", "response": {"type": "order", "data": {"statuses": [5]}}}):
            with self.subTest(payload=payload), self.assertRaises(HyperliquidError):
                parse_exchange_response(payload)

    def test_outcome_classification(self):
        self.assertEqual(classify([{"state": "resting"}], 1), "confirmed")
        self.assertEqual(classify([{"state": "filled"}], 1), "confirmed")
        self.assertEqual(classify([{"state": "error"}], 1), "rejected")
        self.assertEqual(classify([{"state": "error"}], 1), "rejected")
        self.assertEqual(classify([{"state": "resting"}, {"state": "error"}], 2), "partial")
        self.assertEqual(classify([{"state": "unknown"}], 1), "unknown")
        self.assertEqual(classify([], 1), "unknown")
        self.assertEqual(classify([{"state": "resting"}], 2), "unknown")
        self.assertEqual(classify([{"state": "resting"}] * 2, 1), "unknown")


class NonceTests(unittest.TestCase):
    def test_nonces_are_strictly_increasing_even_on_a_frozen_clock(self):
        source = NonceSource(clock=lambda: 1789114000.0)
        values = [source.next() for _ in range(50)]
        self.assertEqual(values, sorted(values))
        self.assertEqual(len(set(values)), 50)

    def test_nonce_follows_the_clock_when_time_moves_faster(self):
        now = [1789114000.0]
        source = NonceSource(clock=lambda: now[0])
        first = source.next()
        now[0] += 5
        self.assertGreater(source.next(), first + 4000)

    def test_nonce_is_within_the_exchange_window(self):
        value = NonceSource().next()
        self.assertGreater(value, 1_600_000_000_000)
        self.assertLess(value, 1 << 64)


class TraderTests(unittest.TestCase):
    def test_incomplete_or_wrong_response_never_confirms_a_write(self):
        responses = [
            {"type": "default"}, {"type": "order"},
            {"type": "order", "data": {"statuses": []}},
            {"type": "order", "data": {"statuses": "success"}},
            {"type": "cancel", "data": {"statuses": ["success"]}},
            {"type": "order", "data": {"statuses": ["success"]}},
            {"type": "order", "data": {"statuses": [{"resting": {"oid": 7}}] * 2}},
            *({"type": "order", "data": {"statuses": [row]}} for row in (
                {"resting": {"oid": True}}, {"resting": {"oid": 2**64}},
                {"resting": {"oid": 7}, "error": "mixed"}, {"error": None},
                {"filled": {"oid": 1, "totalSz": "NaN", "avgPx": "1"}},
                {"filled": {"oid": 1, "totalSz": "1", "avgPx": "0"}},
                {"filled": {"oid": 1, "totalSz": "21", "avgPx": "0.6"}},
                {"filled": {"totalSz": "1", "avgPx": "1"}},
            )),
        ]
        for response in responses:
            with self.subTest(response=response):
                trader = make_trader(response={"status": "ok", "response": response})
                result = trader.place("HL_APT", "buy", 20, 0.6)
                self.assertEqual(result["outcome"], "unknown")
                self.assertTrue(result["uncertain"])
                self.assertEqual(len(trader.transport.payloads), 1, "never replay an ambiguous write")

    def test_place_signs_one_action_with_instrument_precision(self):
        trader = make_trader()
        result = trader.place("HL_APT", "buy", 2.999, 0.6345678, tif="alo", reduce_only=False,
                              cloid="0x" + "2" * 32)
        payload = trader.transport.payloads[0]
        self.assertEqual(payload["action"]["type"], "order")
        order = payload["action"]["orders"][0]
        self.assertEqual((order["a"], order["p"], order["s"], order["c"]),
                         (1, "0.6346", "2.99", "0x" + "2" * 32))
        self.assertEqual(len(payload["action"]["orders"]), 1)
        self.assertEqual(set(payload), {"action", "nonce", "signature"})
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(result["rows"], [{"state": "resting", "oid": 77738308}])

    def test_setting_expiry_is_signed_sent_and_elapsed_requests_are_not_sent(self):
        trader = make_trader(response={"status": "ok", "response": {"type": "default"}})
        action = {"type": "updateLeverage", "asset": 1, "isCross": True, "leverage": 5}
        expiry = int(time.time() * 1000) + 10000
        self.assertEqual(trader.submit(action, expires_after=expiry)["outcome"], "confirmed")
        payload = trader.transport.payloads[0]
        self.assertEqual(payload["expiresAfter"], expiry)
        recovered = recover_agent_or_user_from_l1_action(action, payload["signature"], None, payload["nonce"], expiry, False)
        self.assertEqual(recovered.lower(), SIGNER)
        with self.assertRaises(HyperliquidError):
            trader.submit(action, expires_after=int(time.time() * 1000) - 1)
        self.assertEqual(len(trader.transport.payloads), 1)

    def test_signature_recovers_to_the_api_wallet_for_the_real_payload(self):
        trader = make_trader()
        trader.place("HL_BTC", "sell", 1, 100000, tif="gtc", reduce_only=True)
        payload = trader.transport.payloads[0]
        signature = payload["signature"]
        recovered = recover_agent_or_user_from_l1_action(payload["action"], signature, None,
            payload["nonce"], None, trader.network == "mainnet").lower()
        self.assertEqual(recovered, SIGNER)
        self.assertEqual(recovered, trader.address)

    def test_network_is_bound_into_the_signature(self):
        # Frozen clock so both requests share an action and a nonce: the only
        # difference left is the network source code inside the digest.
        frozen = lambda: NonceSource(clock=lambda: 1789114000.0)
        testnet = make_trader(network="testnet", nonce=frozen())
        mainnet = make_trader(network="mainnet", nonce=frozen())
        testnet.place("HL_APT", "buy", 1, 0.6, tif="alo")
        mainnet.place("HL_APT", "buy", 1, 0.6, tif="alo")
        first, second = testnet.transport.payloads[0], mainnet.transport.payloads[0]
        self.assertEqual(first["action"], second["action"])
        self.assertEqual(first["nonce"], second["nonce"])
        self.assertNotEqual(first["signature"], second["signature"], "the source code must affect the digest")
        # The same signature must verify only under the network it was made for.
        testnet = recover_agent_or_user_from_l1_action(first["action"], first["signature"], None, first["nonce"], None, False)
        mainnet = recover_agent_or_user_from_l1_action(first["action"], first["signature"], None, first["nonce"], None, True)
        self.assertEqual(testnet.lower(), SIGNER)
        self.assertNotEqual(mainnet.lower(), SIGNER)

    def test_fund_moving_actions_cannot_be_signed_or_sent(self):
        trader = make_trader()
        for action in ({"type": "withdraw3", "amount": "1", "destination": ACCOUNT},
                       {"type": "usdClassTransfer", "amount": "1"},
                       {"type": "approveAgent", "agentAddress": ACCOUNT},
                       {"type": "sendAsset", "token": "USDC"},
                       {"type": "spotSend", "token": "PURR"}):
            with self.subTest(action=action["type"]), self.assertRaisesRegex(HyperliquidError, "unsupported action"):
                trader._submit(action)
        self.assertEqual(trader.transport.payloads, [])
        self.assertNotIn("withdraw3", ALLOWED_ACTIONS)
        self.assertNotIn("approveAgent", ALLOWED_ACTIONS)

    def test_transport_failure_is_unknown_and_never_retried(self):
        trader = make_trader(error=HyperliquidError("Exchange request failed: timeout"))
        result = trader.place("HL_APT", "buy", 1, 0.6, tif="alo")
        self.assertEqual(result["outcome"], "unknown")
        self.assertTrue(result["uncertain"], "a lost response may still have been applied")
        self.assertEqual(len(trader.transport.payloads), 1, "a failed write is never silently replayed")

    def test_definitive_http_rejection_is_not_reported_as_uncertain(self):
        trader = make_trader(error=HyperliquidRejectedError("Exchange rejected the request with HTTP 422"))
        result = trader.place("HL_APT", "buy", 1, 0.6, tif="alo")
        self.assertEqual(result["outcome"], "rejected")
        self.assertNotIn("uncertain", result)
        self.assertEqual(len(trader.transport.payloads), 1)

    def test_top_level_error_response_is_rejected_not_unknown(self):
        trader = make_trader(response={"status": "err", "response": "L1 error: Insufficient margin"})
        result = trader.place("HL_APT", "buy", 1, 0.6, tif="alo")
        self.assertEqual(result["outcome"], "rejected")
        self.assertIn("Insufficient margin", result["error"])
        self.assertNotIn("uncertain", result)

    def test_unclassifiable_response_is_unknown_not_success(self):
        trader = make_trader(response={"status": "ok", "response": {"type": "order", "data": {"statuses": []}}})
        trader.transport.response = {"unexpected": True}
        result = trader.place("HL_APT", "buy", 1, 0.6, tif="alo")
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("could not be classified", result["error"])

    def test_exchange_rejection_is_reported_with_its_message(self):
        trader = make_trader(response={"status": "ok", "response": {"type": "order", "data": {
            "statuses": [{"error": "Insufficient margin to place order."}]}}})
        result = trader.place("HL_APT", "buy", 1, 0.6, tif="alo")
        self.assertEqual(result["outcome"], "rejected")
        self.assertIn("Insufficient margin", result["error"])
        self.assertNotIn("response", {k: v for k, v in result.items() if k == "outcome"})

    def test_cancel_requires_asset_index_and_order_id(self):
        trader = make_trader(response={"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}})
        trader.cancel({"asset": 1, "oid": 77738308})
        self.assertEqual(trader.transport.payloads[0]["action"], {"type": "cancel", "cancels": [{"a": 1, "o": 77738308}]})
        for bad in (77738308, -1, {"asset": 1}, {"oid": 1}):
            with self.subTest(bad=bad), self.assertRaises(HyperliquidError):
                trader.cancel(bad)

    def test_cancel_by_client_id_and_modify(self):
        trader = make_trader(response={"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}})
        trader.cancel_by_client_id("HL_APT", "0x" + "3" * 32)
        self.assertEqual(trader.transport.payloads[0]["action"]["cancels"], [{"asset": 1, "cloid": "0x" + "3" * 32}])
        trader.modify({"asset": 1, "oid": 5}, "HL_APT", "sell", 1.999, 0.61234)
        action = trader.transport.payloads[1]["action"]
        self.assertEqual(action["type"], "modify")
        self.assertEqual((action["oid"], action["order"]["s"], action["order"]["p"]), (5, "1.99", "0.6123"))
        with self.assertRaises(HyperliquidError):
            trader.cancel_by_client_id("HL_APT", "0xbad")

    def test_leverage_is_bounded_by_the_asset(self):
        trader = make_trader(response={"status": "ok", "response": {"type": "default"}})
        trader.set_leverage("HL_APT", 5)
        self.assertEqual(trader.transport.payloads[0]["action"],
                         {"type": "updateLeverage", "asset": 1, "isCross": True, "leverage": 5})
        for bad in (0, 11, 1.5, "5", True):
            with self.subTest(bad=bad), self.assertRaises(HyperliquidError):
                trader.set_leverage("HL_APT", bad)

    def test_unknown_symbol_fails_before_signing(self):
        trader = make_trader()
        with self.assertRaises(HyperliquidError):
            trader.place("HL_NOPE", "buy", 1, 1)
        self.assertEqual(trader.transport.payloads, [])

    def test_wrong_network_or_account_configuration_is_refused_early(self):
        with self.assertRaises(HyperliquidError):
            HyperliquidTrader(MARKETS.get, transport=RecordingTransport(), private_key=FOUNDRY_KEY,
                              account_address=ACCOUNT, network="devnet")
        for bad_account in ("", "0x0", "ab" * 20, "0x" + "0" * 40):
            with self.subTest(account=bad_account), self.assertRaises(HyperliquidError):
                HyperliquidTrader(MARKETS.get, transport=RecordingTransport(), private_key=FOUNDRY_KEY,
                                  account_address=bad_account)


class GateTests(unittest.TestCase):
    def test_off_by_default_and_reports_why(self):
        credentials = load_trading_credentials({})
        self.assertEqual(credentials["mode"], "off")
        self.assertIn("disabled", credentials["reason"])
        trader, reason = build_trader(MARKETS.get, environ={})
        self.assertIsNone(trader)
        self.assertIn("disabled", reason)

    def test_invalid_mode_fails_closed(self):
        for mode in ("yes", "prod", "main", "1", "true"):
            with self.subTest(mode=mode):
                self.assertEqual(load_trading_credentials({"HYPERLIQUID_TRADING": mode})["mode"], "off")

    def test_enabling_requires_a_valid_key_and_account(self):
        base = {"HYPERLIQUID_TRADING": "testnet"}
        self.assertIn("SECRET_KEY", load_trading_credentials(base)["reason"])
        self.assertIn("ACCOUNT_ADDRESS",
                      load_trading_credentials({**base, "HYPERLIQUID_SECRET_KEY": FOUNDRY_KEY})["reason"])
        short = load_trading_credentials({**base, "HYPERLIQUID_SECRET_KEY": "0x1234",
                                          "HYPERLIQUID_ACCOUNT_ADDRESS": ACCOUNT})
        self.assertEqual(short["mode"], "off")
        self.assertIn("invalid", short["reason"])
        ready = load_trading_credentials({**base, "HYPERLIQUID_SECRET_KEY": FOUNDRY_KEY,
                                          "HYPERLIQUID_ACCOUNT_ADDRESS": ACCOUNT.upper()})
        self.assertEqual(ready["mode"], "testnet")
        self.assertEqual(ready["host"], "api.hyperliquid-testnet.xyz")
        self.assertEqual(ready["signer_address"], SIGNER)
        self.assertEqual(ready["account_address"], ACCOUNT)

    def test_mainnet_gate_selects_the_mainnet_host(self):
        ready = load_trading_credentials({"HYPERLIQUID_TRADING": "mainnet", "HYPERLIQUID_SECRET_KEY": FOUNDRY_KEY,
                                          "HYPERLIQUID_ACCOUNT_ADDRESS": ACCOUNT})
        self.assertEqual(ready["mode"], "mainnet")
        self.assertEqual(ready["host"], "api.hyperliquid.xyz")

    def test_disabled_gate_never_constructs_a_transport(self):
        def explode(host):
            raise AssertionError("no transport may be built while trading is disabled")
        trader, reason = build_trader(MARKETS.get, environ={}, transport_factory=explode)
        self.assertIsNone(trader)
        self.assertIn("disabled", reason)

    def test_agent_wallet_mismatch_is_disclosed_not_hidden(self):
        trader, note = build_trader(MARKETS.get, environ={"HYPERLIQUID_TRADING": "testnet",
                                                         "HYPERLIQUID_SECRET_KEY": FOUNDRY_KEY,
                                                         "HYPERLIQUID_ACCOUNT_ADDRESS": ACCOUNT},
                                    transport_factory=lambda host: RecordingTransport())
        self.assertIsNotNone(trader)
        self.assertIn("signs for account", note)
        self.assertIn(SIGNER, note)


class DiagnosticsTests(unittest.TestCase):
    def test_identity_check_reports_a_recovered_address_mismatch_clearly(self):
        trader = make_trader(response={"status": "err",
                                       "response": f"L1 error: User or API Wallet {SIGNER} does not exist."})
        result = check_signer_identity(trader, "HL_APT")
        self.assertEqual(result["status"], "err")
        self.assertEqual(result["expected"], SIGNER)
        self.assertEqual(result["recovered"], SIGNER)
        self.assertTrue(result["matches"])
        self.assertEqual(len(trader.transport.payloads), 1)

    def test_identity_check_detects_a_wrong_signer(self):
        trader = make_trader(response={"status": "err",
                                       "response": "L1 error: User or API Wallet 0x" + "cd" * 20 + " does not exist."})
        result = check_signer_identity(trader, "HL_APT")
        self.assertFalse(result["matches"])
        self.assertNotEqual(result["recovered"], trader.address)

    def test_identity_check_sends_a_cancelable_post_only_order(self):
        trader = make_trader(response={"status": "err", "response": "no wallet"})
        check_signer_identity(trader, "HL_APT")
        action = trader.transport.payloads[0]["action"]
        self.assertEqual(action["orders"][0]["t"], {"limit": {"tif": "Alo"}})
        self.assertFalse(action["orders"][0]["r"])

    def test_authorization_check_cancels_an_order_id_that_cannot_exist(self):
        from hyperliquid_trading import UNREACHABLE_OID, check_signer_authorization
        for message, verdict in (("User or API Wallet 0x1 does not exist.", "unapproved"),
                                 ("Order was never placed, already canceled, or filled.", "approved"),
                                 ("something else entirely", "unknown")):
            with self.subTest(verdict=verdict):
                trader = make_trader(response={"status": "err", "response": message})
                result = check_signer_authorization(trader, "HL_APT")
                self.assertEqual(result["verdict"], verdict)
                action = trader.transport.payloads[0]["action"]
                self.assertEqual(action["type"], "cancel")
                self.assertEqual(action["cancels"], [{"a": 1, "o": UNREACHABLE_OID}])
                self.assertEqual(set(action), {"type", "cancels"}, "must never build an order")
                self.assertGreater(UNREACHABLE_OID, 2 ** 32)


class FakeTrader:
    network = "testnet"
    account_address = ACCOUNT
    address = SIGNER

    def __init__(self):
        self.calls = []

    def submit(self, action, count=1, *, expires_after=None):
        self.expiry = expires_after
        self.calls.append((action, count))
        return {"outcome": "confirmed", "action": action, "nonce": 1789114000000,
                "rows": [{"state": "resting", "oid": 1}]}


class FakeMarkets:
    network = "testnet"
    account_address = ACCOUNT

    def __init__(self):
        self.require_agent = Mock()
        self.validate_close = Mock()

    def markets(self):
        return {"HL_APT": {"instrument": {"symbol": "HL_APT", "assetId": 1, "contractValueTradePrecision": 2,
                                          "tradeable": True, "maxLeverage": 10}},
                "HL_GONE": {"instrument": {"symbol": "HL_GONE", "assetId": 9, "contractValueTradePrecision": 0,
                                           "tradeable": False}}}


def server_write_helpers():
    """Extract the venue write helpers from server.py without importing it (it starts threads)."""
    source = ast.parse(Path(__file__).with_name("server.py").read_text(encoding="utf-8"))
    wanted = {"_hl_instrument", "hyperliquid_trader", "hyperliquid_order_action", "hyperliquid_leverage_intent",
              "hyperliquid_cancel_action", "hyperliquid_write", "_as_float", "hyperliquid_gate",
              "ensure_agent", "forget_agent_if_refused", "_agent_fresh"}
    body = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert len(body) == len(wanted), "server write helpers were renamed; update this test"
    # The signer-approval cache state those helpers share, fresh for every namespace.
    state = ast.parse("\n".join([
        "import threading",
        "AGENT_CHECK_TTL = 300.0",
        "_agent_checked = {'signer': None, 'at': 0.0}",
        "_agent_lock = threading.Lock()",
    ])).body
    return ast.fix_missing_locations(ast.Module(body=state + body, type_ignores=[]))


class WriteDecisionTests(unittest.TestCase):
    """The seam that decides between validating, simulating, and signing."""

    def setUp(self):
        from typing import Any as _Any
        import hyperliquid_trading as trading
        import hyperliquid_recovery
        self.trader = FakeTrader()
        self.db = Mock()
        self.ns = {"Any": _Any, "json": json, "time": time, "hyperliquid_trading": trading, "hyperliquid_recovery": hyperliquid_recovery,
                   "hyperliquid": FakeMarkets(), "db": self.db, "arm_lock": threading.RLock(),
                   "armed": False, "_hl_trader": {"ready": True, "trader": self.trader, "reason": None},
                   "_hl_gate": {},
                   "READ_ONLY_MESSAGE": trading_ready_message(),
                   "HL_TIF": {"mkt": "ioc", "lmt": "gtc", "post": "alo", "ioc": "ioc"},
                   "HL_TRIGGER": {"stp": "sl", "take_profit": "tp"}}
        exec(compile(server_write_helpers(), "isolated_server_writes", "exec"), self.ns)

    def write(self, path, body):
        return self.ns["hyperliquid_write"](path, body)

    def order(self, **changes):
        body = {"symbol": "HL_APT", "side": "buy", "orderType": "lmt", "size": 2.999, "limitPrice": 0.6345678}
        body.update(changes)
        return body

    def test_disarmed_validates_and_simulates_without_signing_or_submitting(self):
        result = self.write("/api/order", self.order())
        self.assertEqual(result["outcome"], "simulated")
        self.assertTrue(result["simulated"])
        self.assertFalse(result["live"])
        self.assertEqual(result["exchange"], "hyperliquid")
        self.assertIn("DISARMED", result["message"])
        self.assertEqual(self.trader.calls, [])
        self.db.log_action.assert_not_called()

    def test_armed_submits_the_exact_action_the_disarmed_preview_built(self):
        preview = self.write("/api/order", self.order())["action"]
        self.ns["armed"] = True
        live = self.write("/api/order", self.order())
        self.assertTrue(live["live"])
        self.assertEqual(live["outcome"], "confirmed")
        self.assertEqual(live["action"], preview)
        self.assertEqual(self.trader.calls[0][0], preview)
        self.assertEqual(self.trader.calls[0][1], 1)

    def test_disarm_during_trader_setup_prevents_submission(self):
        self.ns["armed"] = True
        def setup():
            self.ns["armed"] = False
            return self.trader, None
        self.ns["hyperliquid_trader"] = setup
        result = self.write("/api/order", self.order())
        self.assertEqual(result["outcome"], "simulated")
        self.assertEqual(self.trader.calls, [])

    def test_close_requires_reduce_only_ioc_and_checks_prepared_size_before_submission(self):
        body = {**self.order(), "orderType": "ioc", "reduceOnly": True, "closePosition": True, "size": 2.009, "limitPrice": 0.6}
        result = self.write("/api/order", body)
        self.assertTrue(result["simulated"])
        self.ns["hyperliquid"].validate_close.assert_called_with("HL_APT", body["side"], "2", positions=None)
        self.assertEqual(self.trader.calls, [])
        self.ns["armed"] = True
        self.ns["hyperliquid"].validate_close.side_effect = HyperliquidError("position changed")
        with self.assertRaisesRegex(HyperliquidError, "position changed"):
            self.write("/api/order", body)
        self.assertEqual(self.trader.calls, [])
        for patch in ({"orderType": "lmt"}, {"reduceOnly": False}, {"closePosition": "true"}):
            with self.subTest(patch=patch), self.assertRaises(HyperliquidError):
                self.write("/api/order", {**body, **patch})
        for side, price in (("buy", 0.60006), ("sell", 0.60004)):
            with self.subTest(side=side), self.assertRaisesRegex(HyperliquidError, "price bound"):
                self.write("/api/order", {**body, "side": side, "limitPrice": price})

    def test_market_close_reuses_quote_bound_and_checks_arm_and_exact_snapshot(self):
        self.ns['hyperliquid'].quote = Mock(return_value={'time': int(time.time()*1000),
            'orderBook': {'bids': [[0.59, 100]], 'asks': [[0.61, 100]]}})
        position = {'side': 'short', 'sizeExact': '2', 'price': 0.8}
        body = self.order(orderType='mkt', limitPrice=None, size=2, reduceOnly=True, closePosition=True,
                          position=position, expectedArmed=False, slippagePercent=0.5)
        result = self.write('/api/order', body)
        self.assertTrue(result['simulated'])
        self.ns['hyperliquid'].validate_close.assert_called_with('HL_APT', 'buy', '2', position, positions=None)
        wire = result['action']['orders'][0]
        self.assertTrue(wire['r'])
        self.assertEqual(wire['t'], {'limit': {'tif': 'Ioc'}})
        self.assertLessEqual(Decimal(wire['p']), Decimal('0.61') * Decimal('1.005'))
        self.ns['armed'] = True
        with self.assertRaisesRegex(HyperliquidError, 'ARM state'): self.write('/api/order', body)
        self.assertEqual(self.trader.calls, [])
        body['expectedArmed'] = True
        self.assertEqual(self.write('/api/order', body)['outcome'], 'confirmed')
        self.assertEqual(len(self.trader.calls), 1)
        self.ns['hyperliquid'].validate_close.side_effect = HyperliquidError('position changed')
        with self.assertRaisesRegex(HyperliquidError, 'position changed'): self.write('/api/order', body)
        self.assertEqual(len(self.trader.calls), 1)
        for change in ({'position': None}, {'expectedArmed': None}, {'reduceOnly': False}, {'size': True}):
            with self.assertRaises(HyperliquidError): self.write('/api/order', {**body, **change})

    def test_close_runs_real_binding_and_position_checks_before_the_mock_exchange(self):
        from types import SimpleNamespace
        from hyperliquid_backend import HyperliquidBackend
        calls = []
        position = {"coin": "APT", "szi": "-2", "entryPx": "0.8", "unrealizedPnl": "0"}
        def info(kind, **params):
            calls.append(kind)
            if kind == "extraAgents":
                return [{"address": SIGNER, "validUntil": 9999999999999}]
            if kind == "clearinghouseState":
                return {"marginSummary": {}, "assetPositions": [{"position": dict(position)}]}
            raise AssertionError(kind)
        backend = HyperliquidBackend(Mock(), client=SimpleNamespace(info=info), network="testnet",
                                     account_address=ACCOUNT, enable_feed=False)
        backend.markets = lambda: {symbol: {"instrument": {**instrument, "tradeable": True}}
                                   for symbol, instrument in MARKETS.items()}
        trader = make_trader(response={"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"oid": 1, "totalSz": "1", "avgPx": "0.6"}}]}}})
        self.ns["hyperliquid"] = backend
        self.ns["_hl_trader"] = {"ready": True, "trader": trader, "reason": None}
        self.ns["armed"] = True
        body = self.order(orderType="ioc", reduceOnly=True, closePosition=True, size=2, limitPrice=0.6)
        result = self.write("/api/order", body)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(calls, ["extraAgents", "clearinghouseState"])
        self.assertEqual(len(trader.transport.payloads), 1)
        action = trader.transport.payloads[0]["action"]["orders"][0]
        self.assertTrue(action["r"])
        self.assertEqual(action["t"], {"limit": {"tif": "Ioc"}})
        position["szi"] = "-1"
        with self.assertRaises(HyperliquidError):
            self.write("/api/order", body)
        self.assertEqual(len(trader.transport.payloads), 1, "changed exposure must not reach the exchange")

    def test_usd_budget_checks_final_rounded_price_before_submission(self):
        self.ns["armed"] = True
        body = self.order(size=100, limitPrice=0.60006, maxNotional="60.006")
        with self.assertRaisesRegex(HyperliquidError, "exceeds the USD"):
            self.write("/api/order", body)
        self.assertEqual(self.trader.calls, [])
        with self.assertRaises(HyperliquidError):
            self.write("/api/order", {**body, "maxNotional": "60.0099999999999999999"})
        result = self.write("/api/order", {**body, "maxNotional": "60.01"})
        self.assertEqual(result["action"]["orders"][0]["p"], "0.6001")
        for value in (None, True, 0, -1, "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(HyperliquidError):
                self.write("/api/order", {**body, "maxNotional": value})
        self.assertEqual(len(self.trader.calls), 1)

    def test_notional_budget_uses_normalized_size_and_refuses_market_triggers(self):
        result = self.write("/api/order", self.order(size=2.999, limitPrice=0.6, maxNotional="1.794"))
        self.assertEqual(result["action"]["orders"][0]["s"], "2.99")
        body = self.order(orderType="stp", reduceOnly=True, size=100, stopPrice=0.60006,
                          limitPrice=0.60006, maxNotional="60.006")
        with self.assertRaisesRegex(HyperliquidError, "exceeds the USD"):
            self.write("/api/order", body)
        for market in (True, "false"):
            with self.subTest(market=market), self.assertRaises(HyperliquidError):
                self.write("/api/order", {**body, "triggerMarket": market, "maxNotional": 100})
        self.assertEqual(self.trader.calls, [])

    def test_binding_failure_or_identity_mismatch_never_submits(self):
        self.ns["armed"] = True
        self.ns["hyperliquid"].require_agent.side_effect = HyperliquidError("agent revoked")
        with self.assertRaisesRegex(HyperliquidError, "revoked"):
            self.write("/api/order", self.order())
        self.assertEqual(self.trader.calls, [])
        self.ns["hyperliquid"].require_agent.reset_mock()
        for name, value in (("network", "mainnet"), ("account_address", "0x" + "cd" * 20)):
            previous = getattr(self.trader, name)
            setattr(self.trader, name, value)
            with self.subTest(name=name), self.assertRaisesRegex(HyperliquidError, "identities disagree"):
                self.write("/api/order", self.order())
            setattr(self.trader, name, previous)
        self.ns["hyperliquid"].require_agent.assert_not_called()
        self.assertEqual(self.trader.calls, [])

    def test_armed_with_trading_disabled_still_simulates_and_says_why(self):
        self.ns["armed"] = True
        self.ns["_hl_trader"] = {"ready": True, "trader": None,
                                 "reason": "Signed trading is disabled (HYPERLIQUID_TRADING=off)"}
        result = self.write("/api/order", self.order())
        self.assertEqual(result["outcome"], "simulated")
        self.assertFalse(result["live"])
        self.assertIn("HYPERLIQUID_TRADING=off", result["message"])

    def test_precision_is_applied_before_any_write(self):
        action = self.write("/api/order", self.order())["action"]
        order = action["orders"][0]
        self.assertEqual((order["s"], order["p"]), ("2.99", "0.6346"))
        self.assertEqual(action["type"], "order")
        self.assertEqual(action["grouping"], "na")

    def test_order_type_mapping(self):
        for order_type, expected in (("lmt", "Gtc"), ("post", "Alo"), ("ioc", "Ioc")):
            with self.subTest(order_type=order_type):
                action = self.write("/api/order", self.order(orderType=order_type))["action"]
                self.assertEqual(action["orders"][0]["t"], {"limit": {"tif": expected}})

    def test_percent_entry_rechecks_directional_capacity_without_double_leverage(self):
        self.ns["hyperliquid"].trading_capacity = Mock(return_value={"leverage": {"value": 5, "type": "cross"},
            "maxTradeSizes": {"buy": "123.456", "sell": "200"}})
        body = self.order(size=1000, quickPercent=25, expectedLeverage=5, expectedMarginMode="cross")
        # Opening sizes use 97% of the venue maximum so a 100% order still fits.
        self.assertEqual(self.write("/api/order", body)["action"]["orders"][0]["s"], "29.93")
        self.assertEqual(self.write("/api/order", {**body, "side": "sell"})["action"]["orders"][0]["s"], "48.5")
        self.assertEqual(self.write("/api/order", {**body, "quickPercent": 100})["action"]["orders"][0]["s"], "119.75")
        self.assertEqual(self.write("/api/order", {**body, "size": 5})["action"]["orders"][0]["s"], "5")
        with self.assertRaisesRegex(HyperliquidError, "leverage changed"):
            self.write("/api/order", {**body, "expectedLeverage": 3})
        self.assertEqual(self.trader.calls, [])

    def test_percent_reduction_uses_position_not_available_margin(self):
        self.ns["hyperliquid"].positions = Mock(return_value={"positions": [{"symbol": "HL_APT", "side": "long", "sizeExact": "10"}]})
        body = self.order(side="sell", size=100, reduceOnly=True, quickPercent=25)
        self.assertEqual(self.write("/api/order", body)["action"]["orders"][0]["s"], "2.5")
        self.ns["hyperliquid"].positions.assert_called_with(fresh=True)
        with self.assertRaises(HyperliquidError):
            self.write("/api/order", {**body, "side": "buy"})

    def test_leverage_preserves_margin_mode_and_persisted_deadline_under_arm_gate(self):
        self.ns["hyperliquid"].trading_capacity = Mock(return_value={"leverage": {"value": 3, "type": "cross"}})
        body = {"symbol": "HL_APT", "leverage": 5, "expectedLeverage": 3, "cross": True, "expectedArmed": False}
        intent = self.ns["hyperliquid_leverage_intent"](body)
        result = self.ns["hyperliquid_write"]("/api/leverage", body, prepared_action=intent)
        self.assertEqual(result["outcome"], "simulated")
        self.assertEqual(self.trader.calls, [])
        self.ns["armed"] = True
        with self.assertRaisesRegex(HyperliquidError, "ARM state changed"):
            self.ns["hyperliquid_write"]("/api/leverage", body, prepared_action=intent)
        body["expectedArmed"] = True
        intent = self.ns["hyperliquid_leverage_intent"](body)
        result = self.ns["hyperliquid_write"]("/api/leverage", body, prepared_action=intent)
        self.assertEqual(self.trader.expiry, intent["expiresAfter"])
        self.assertEqual(self.trader.calls[0][0], {"type": "updateLeverage", "asset": 1, "isCross": True, "leverage": 5})
        self.ns["hyperliquid"].trading_capacity.return_value["leverage"]["type"] = "isolated"
        with self.assertRaisesRegex(HyperliquidError, "margin mode changed"):
            self.ns["hyperliquid_write"]("/api/leverage", body, prepared_action=intent)
        self.assertEqual(len(self.trader.calls), 1)
        for patch in ({"leverage": True}, {"leverage": 11}, {"cross": "true"}):
            with self.assertRaises(HyperliquidError):
                self.ns["hyperliquid_leverage_intent"]({**body, **patch})

    def test_market_buy_sell_use_fresh_quotes_and_conservative_bounds(self):
        book = {"time": time.time() * 1000, "orderBook": {"bids": [[0.63, 10]], "asks": [[0.6345, 10]]}}
        self.ns["hyperliquid"].quote = Mock(return_value=book)
        for side, quote in (("buy", "0.6345"), ("sell", "0.63")):
            body = self.order(orderType="mkt", side=side, limitPrice=None, slippagePercent=0.5,
                              maxNotional="1", cloid="0x" + "a" * 32)
            preview = self.write("/api/order", body)
            wire = preview["action"]["orders"][0]
            bound = Decimal(quote) * (Decimal("1.005") if side == "buy" else Decimal("0.995"))
            self.assertTrue(Decimal(wire["p"]) <= bound if side == "buy" else Decimal(wire["p"]) >= bound)
            self.assertLessEqual(Decimal(wire["p"]) * Decimal(wire["s"]), Decimal("1"))
            self.assertEqual(wire["t"], {"limit": {"tif": "Ioc"}})
            self.assertEqual(wire["c"], body["cloid"])
            self.assertEqual(self.trader.calls, [])
        self.ns["hyperliquid"].quote.assert_called_with("HL_APT")
        self.ns["armed"] = True
        live = self.write("/api/order", body)
        self.assertEqual(live["action"], preview["action"])
        self.assertEqual(len(self.trader.calls), 1)

    def test_market_rejects_bad_slippage_stale_or_missing_quotes_and_manual_price(self):
        book = {"time": time.time() * 1000, "orderBook": {"bids": [[0.63, 10]], "asks": [[0.64, 10]]}}
        self.ns["hyperliquid"].quote = Mock(return_value=book)
        for slip in (True, 0, -1, 6, "bad", float("nan")):
            with self.assertRaises(HyperliquidError):
                self.write("/api/order", self.order(orderType="mkt", limitPrice=None, slippagePercent=slip))
        with self.assertRaises(HyperliquidError):
            self.write("/api/order", self.order(orderType="mkt"))
        for bad in ({}, {**book, "time": 1}, {**book, "orderBook": {"bids": [], "asks": []}},
                    {**book, "orderBook": {"bids": [[1, 1]], "asks": [[0.5, 1]]}}):
            self.ns["hyperliquid"].quote.return_value = bad
            with self.assertRaises(HyperliquidError):
                self.write("/api/order", self.order(orderType="mkt", limitPrice=None))
        self.assertEqual(self.trader.calls, [])

    def test_trigger_orders_are_reduce_only_and_carry_the_kind(self):
        for order_type, kind in (("stp", "sl"), ("take_profit", "tp")):
            with self.subTest(order_type=order_type):
                action = self.write("/api/order", self.order(orderType=order_type, stopPrice=0.7,
                                                            reduceOnly=True))["action"]
                order = action["orders"][0]
                self.assertEqual(order["t"], {"trigger": {"isMarket": False, "triggerPx": "0.7", "tpsl": kind}})
                self.assertTrue(order["r"])
        cloid = "0x" + "a" * 32
        action = self.write("/api/order", self.order(orderType="stp", stopPrice=0.7,
                                                   reduceOnly=True, cloid=cloid))["action"]
        self.assertEqual(action["orders"][0]["c"], cloid)
        with self.assertRaisesRegex(HyperliquidError, "reduce-only"):
            self.write("/api/order", self.order(orderType="stp", stopPrice=0.7))
        with self.assertRaisesRegex(HyperliquidError, "stopPrice"):
            self.write("/api/order", self.order(orderType="stp", reduceOnly=True))

    def test_order_validation_fails_before_any_signing(self):
        cases = [(self.order(side="long"), "side"), (self.order(size=0), "size"),
                 (self.order(size="abc"), "size"), (self.order(reduceOnly="true"), "reduceOnly"),
                 (self.order(limitPrice=None), "limitPrice"), (self.order(limitPrice=0), "limitPrice"),
                 (self.order(orderType="twap"), "orderType"), (self.order(symbol="HL_GONE"), "Unknown"),
                 (self.order(symbol="HL_NOPE"), "Unknown")]
        for body, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(HyperliquidError, message):
                self.write("/api/order", body)
        self.assertEqual(self.trader.calls, [])

    def test_cancel_by_client_id_and_by_asset_identity(self):
        cloid = "0x" + "4" * 32
        self.assertEqual(self.write("/api/cancel", {"symbol": "HL_APT", "cliOrdId": cloid})["action"],
                         {"type": "cancelByCloid", "cancels": [{"asset": 1, "cloid": cloid}]})
        self.assertEqual(self.write("/api/cancel", {"asset": 1, "orderId": 77738308})["action"],
                         {"type": "cancel", "cancels": [{"a": 1, "o": 77738308}]})
        for bad in ({}, {"asset": 1}, {"orderId": 1}, {"asset": -1, "orderId": 1},
                    {"asset": "1", "orderId": 1}, {"symbol": "HL_NOPE", "cliOrdId": cloid}):
            with self.subTest(bad=bad), self.assertRaises(HyperliquidError):
                self.write("/api/cancel", bad)

    def test_cancel_browser_symbol_and_exact_decimal_id(self):
        oid = "12345678901234567890"
        action = self.write("/api/cancel", {"symbol": "HL_APT", "orderId": oid})["action"]
        self.assertEqual(action, {"type": "cancel", "cancels": [{"a": 1, "o": int(oid)}]})
        for changes in ({"asset": 2}, {"orderId": True}, {"orderId": "0"},
                        {"orderId": str(2**64)}, {"orderId": "1e3"},
                        {"cliOrdId": "0xbad"}):
            with self.subTest(changes=changes), self.assertRaises(HyperliquidError):
                self.write("/api/cancel", {"symbol": "HL_APT", "orderId": oid, **changes})
        self.assertEqual(self.trader.calls, [])

    def test_account_cancel_validates_all_targets_before_any_submission(self):
        original = self.ns["hyperliquid"].markets()
        self.ns["hyperliquid"].markets = lambda: {**original, "HL_BTC": {"instrument": {**MARKETS["HL_BTC"], "tradeable": True}}}
        targets = [{"symbol": "HL_APT", "orderId": "12345678901234567890"}, {"symbol": "HL_BTC", "orderId": "2"}]
        result = self.write("/api/cancel", {"targets": targets})
        self.assertEqual(result["action"]["cancels"], [{"a": 1, "o": 12345678901234567890}, {"a": 0, "o": 2}])
        for body in ({"targets": []}, {"targets": targets, "symbol": "HL_APT"},
                     {"targets": [targets[0], dict(targets[1], orderId=targets[0]["orderId"])]},
                     {"targets": [dict(targets[0], orderId=123)]},
                     {"targets": [dict(targets[0], symbol="PF_APTUSD")]}, {"targets": targets * 51}):
            with self.subTest(body=body), self.assertRaises(HyperliquidError):
                self.write("/api/cancel", body)
        self.assertEqual(self.trader.calls, [])

    def test_bulk_cancel_freezes_exact_ids_and_rejects_ambiguous_targets(self):
        ids = ["12345678901234567890", "2"]
        result = self.write("/api/cancel", {"symbol": "HL_APT", "orderIds": ids})
        self.assertEqual(result["action"], {"type": "cancel", "cancels": [
            {"a": 1, "o": int(ids[0])}, {"a": 1, "o": 2}]})
        self.assertTrue(result["simulated"])
        for changes in ({"orderIds": []}, {"orderIds": [1]}, {"orderIds": ["0"]},
                        {"orderIds": ["1", "01"]}, {"orderIds": [str(2**64)]},
                        {"orderIds": [str(n) for n in range(1, 102)]}, {"orderId": "3"},
                        {"cliOrdId": "0x" + "a" * 32}, {"asset": 2}):
            with self.subTest(changes=changes), self.assertRaises(HyperliquidError):
                self.write("/api/cancel", {"symbol": "HL_APT", "orderIds": ids, **changes})
        self.assertEqual(self.trader.calls, [])

    def test_cancel_count_is_reported_to_the_classifier(self):
        self.ns["armed"] = True
        self.write("/api/cancel", {"asset": 1, "orderId": 5})
        self.assertEqual(self.trader.calls[0][1], 1)

    def test_gate_view_never_exposes_key_material(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"HYPERLIQUID_TRADING": "testnet", "HYPERLIQUID_SECRET_KEY": FOUNDRY_KEY,
                                     "HYPERLIQUID_ACCOUNT_ADDRESS": ACCOUNT}, clear=False):
            gate = self.ns["hyperliquid_gate"]()
        self.assertEqual(gate["mode"], "testnet")
        self.assertEqual(gate["signer"], SIGNER)
        self.assertEqual(gate["host"], "api.hyperliquid-testnet.xyz")
        serialized = json.dumps(gate)
        self.assertNotIn(FOUNDRY_KEY, serialized)
        self.assertNotIn("private_key", serialized)
        self.assertNotIn("secret", serialized.lower())

    def test_mismatched_read_and_signing_networks_disable_trading(self):
        import os
        from unittest.mock import patch
        self.ns["hyperliquid"].network = "mainnet"
        self.ns["_hl_trader"]["ready"] = False
        with patch.dict(os.environ, {"HYPERLIQUID_TRADING": "testnet", "HYPERLIQUID_SECRET_KEY": FOUNDRY_KEY,
                                     "HYPERLIQUID_ACCOUNT_ADDRESS": ACCOUNT}, clear=False):
            gate = self.ns["hyperliquid_gate"]()
            self.assertEqual(gate["mode"], "off")
            self.assertIn("networks disagree", gate["reason"])
            trader, reason = self.ns["hyperliquid_trader"]()
            self.assertIsNone(trader)
            self.assertIn("networks disagree", reason)

    def test_gate_view_reports_disabled_by_default_without_building_a_trader(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {}, clear=False):
            for name in ("HYPERLIQUID_TRADING", "HYPERLIQUID_SECRET_KEY"):
                os.environ.pop(name, None)
            gate = self.ns["hyperliquid_gate"]()
        self.assertEqual(gate["mode"], "off")
        self.assertIn("disabled", gate["reason"])
        self.assertIsNone(gate["signer"])

    def test_paths_that_are_not_writes_are_refused(self):
        for path in ("/api/chat", "/api/grid", "/api/flatten", "/api/chase"):
            with self.subTest(path=path), self.assertRaises(HyperliquidError):
                self.write(path, {})
        self.assertEqual(self.trader.calls, [])

    def test_client_order_id_passes_through_unchanged(self):
        cloid = "0x" + "5" * 32
        action = self.write("/api/order", self.order(cloid=cloid))["action"]
        self.assertEqual(action["orders"][0]["c"], cloid)
        with self.assertRaises(HyperliquidError):
            self.write("/api/order", self.order(cloid="0xbad"))


def trading_ready_message():
    from hyperliquid_backend import READ_ONLY_MESSAGE
    return READ_ONLY_MESSAGE


class TransportTests(unittest.TestCase):
    def test_exchange_transport_posts_json_to_the_exchange_endpoint(self):
        transport = Mock()
        transport.post.return_value = {"status": "ok", "response": {"type": "default"}}
        payload = ExchangeTransport("api.hyperliquid-testnet.xyz", transport=transport).post({"a": 1})
        transport.post.assert_called_once_with("/exchange", {"a": 1})
        self.assertEqual(payload["status"], "ok")
        with self.assertRaises(HyperliquidError):
            ExchangeTransport("untrusted.example")

    def test_transport_reports_http_and_network_failures(self):
        from hyperliquid.utils.error import ClientError, ServerError
        from requests import ConnectionError
        for error, expected, exception in ((ServerError(502, "bad gateway"), "HTTP 502", HyperliquidError),
                (ConnectionError("offline"), "failed", HyperliquidError),
                (ClientError(429, None, "rate limited", {}), "HTTP 429", HyperliquidRejectedError)):
            transport = Mock()
            transport.post.side_effect = error
            client = ExchangeTransport("api.hyperliquid-testnet.xyz", transport=transport)
            with self.assertRaisesRegex(exception, expected):
                client.post({})
            transport.post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
