import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
from hyperliquid.utils.error import ClientError

from hyperliquid_backend import HyperliquidBackend
from hyperliquid_client import HyperliquidClient, HyperliquidError, INFO_TYPES


class HyperliquidTests(unittest.TestCase):
    def setUp(self):
        context = {"markPx": "0.7", "oraclePx": "0.701", "prevDayPx": "0.65", "funding": "0.00001",
                   "openInterest": "1000", "dayNtlVlm": "2000", "dayBaseVlm": "3000"}
        self.data = {
            "metaAndAssetCtxs": [{"universe": [
                {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                {"name": "APT", "szDecimals": 2, "maxLeverage": 10},
                {"name": "OLD", "szDecimals": 0, "maxLeverage": 3, "isDelisted": True},
            ]}, [{**context, "markPx": "100001"}, context, context]],
            "recentTrades": [{"coin": "APT", "side": "B", "px": "0.6", "sz": "4", "time": 1700000000000, "tid": 10}],
            "l2Book": {"coin": "APT", "time": 1700000000000,
                       "levels": [[{"px": "0.58", "sz": "2"}, {"px": "0.59", "sz": "5"}],
                                  [{"px": "0.61", "sz": "8"}, {"px": "0.60", "sz": "2"}]]},
            "candleSnapshot": [{"s": "APT", "i": "1m", "t": 1700000040000, "o": "0.6", "h": "0.7", "l": "0.5", "c": "0.65", "v": "42"}],
            "clearinghouseState": {"marginSummary": {"totalRawUsd": "100", "accountValue": "110", "totalMarginUsed": "3"},
                                   "withdrawable": "80", "assetPositions": [{"position": {
                                       "coin": "APT", "szi": "-2", "entryPx": "0.8", "unrealizedPnl": "0.2", "liquidationPx": "1.2",
                                       "cumFunding": {"sinceOpen": "0.001"}}}]},
            "frontendOpenOrders": [{"coin": "APT", "oid": 12345678901234567890, "side": "B", "sz": "1.5", "origSz": "2",
                                    "limitPx": "0.4", "triggerPx": "0.5", "isTrigger": True, "orderType": "Take Profit Limit",
                                    "reduceOnly": True, "timestamp": 1700000000000}],
            "userFills": [{"coin": "APT", "side": "A", "px": "0.7", "sz": "2", "time": 1700000000000,
                           "tid": 88, "oid": 12345678901234567890, "fee": "0.01", "feeToken": "USDC", "closedPnl": "0.2", "crossed": True}],
            "spotClearinghouseState": {"balances": []},
            "userAbstraction": "manual",
        }
        self.calls = []

        def info(kind, **params):
            self.assertIn(kind, INFO_TYPES)
            self.calls.append((kind, params))
            return deepcopy(self.data[kind])
        self.client = SimpleNamespace(host="fixture.test", info=Mock(side_effect=info))
        self.publish = Mock()
        self.backend = HyperliquidBackend(self.publish, client=self.client, account_address="0x" + "1" * 40, enable_feed=False)

    def read(self, path, **query):
        return self.backend.read("/api/" + path, query)

    def test_directional_capacity_is_fresh_scoped_and_reports_real_leverage(self):
        self.data["activeAssetData"] = {"coin": "APT", "user": self.backend.account_address,
            "leverage": {"type": "cross", "value": 5}, "maxTradeSzs": ["12.34", "56.78"],
            "availableToTrade": ["1", "2"], "markPx": "0.6"}
        first = self.backend.trading_capacity("HL_APT")
        self.assertEqual(first["maxTradeSizes"], {"buy": "12.34", "sell": "56.78"})
        self.assertEqual(first["leverage"], {"type": "cross", "value": 5})
        self.data["activeAssetData"]["leverage"]["value"] = 3
        self.assertEqual(self.backend.trading_capacity("HL_APT")["leverage"]["value"], 3)
        for patch in ({"coin": "BTC"}, {"user": "0x" + "2" * 40}, {"maxTradeSzs": ["1"]},
                      {"maxTradeSzs": ["-1", "2"]}, {"leverage": {"type": "cross", "value": True}}):
            old = deepcopy(self.data["activeAssetData"])
            self.data["activeAssetData"].update(patch)
            with self.assertRaises(HyperliquidError):
                self.backend.trading_capacity("HL_APT")
            self.data["activeAssetData"] = old

    def test_native_position_tpsl_zero_size_is_not_a_zero_coverage_order(self):
        order = self.data['frontendOpenOrders'][0]
        order.update(sz='0', origSz='0', isPositionTpsl=True)
        normalized = self.backend.orders(fresh=True)['orders'][0]
        self.assertTrue(normalized['positionTpsl'])
        self.assertEqual(normalized['unfilledSizeExact'], '0')
        self.data['orderStatus'] = {'status': 'order', 'order': {
            'order': order, 'status': 'open', 'statusTimestamp': 1700000000000}}
        status = self.backend.order_status(str(order['oid']))
        self.assertTrue(status['positionTpsl'])
        self.assertEqual(status['originalSizeExact'], '0')
        order['isPositionTpsl'] = False
        self.assertFalse(self.backend.orders(fresh=True)['orders'][0]['positionTpsl'])
        order.pop('isPositionTpsl')
        self.assertTrue(self.backend.orders(fresh=True)['orders'][0]['positionTpsl'])
        order.update(sz='2', origSz='2', isPositionTpsl=True)
        self.assertFalse(self.backend.orders(fresh=True)['orders'][0]['positionTpsl'])

    def test_market_close_snapshot_and_fresh_read_reject_changed_exposure(self):
        expected = {'side': 'short', 'sizeExact': '2', 'price': 0.8}
        self.backend.validate_close('HL_APT', 'buy', '2', expected)
        for change in ({'sizeExact': '1'}, {'price': 0.9}, {'side': 'long'}):
            with self.assertRaises(HyperliquidError): self.backend.validate_close('HL_APT', 'buy', '2', {**expected, **change})
        with self.assertRaises(HyperliquidError): self.backend.validate_close('HL_APT', 'buy', '1', expected)
        self.data['clearinghouseState']['assetPositions'] = []
        result, code = self.read('positions', fresh='1')
        self.assertEqual(code, 200)
        self.assertEqual(result['positions'], [])
        with self.assertRaises(HyperliquidError): self.backend.validate_close('HL_APT', 'buy', '2', expected)

    def test_grid_preflight_bypasses_order_and_book_caches(self):
        self.backend.orders()
        self.backend.orderbook("HL_APT")
        self.client.info.reset_mock()
        self.backend.orders()
        self.backend.orderbook("HL_APT")
        self.client.info.assert_not_called()
        orders = self.backend.orders(fresh=True)
        self.backend.orderbook("HL_APT", fresh=True)
        self.assertEqual([call.args[0] for call in self.client.info.call_args_list], ["frontendOpenOrders", "l2Book"])
        self.assertEqual(orders["orders"][0]["unfilledSizeExact"], "1.5")

    def test_order_status_reads_exact_ids_and_never_infers_a_fill(self):
        oid = 12345678901234567890
        cloid = "0x" + "a" * 32
        order = {**self.data["frontendOpenOrders"][0], "oid": oid, "cloid": cloid}
        self.data["orderStatus"] = {"status": "order", "order": {
            "order": order, "status": "open", "statusTimestamp": 1700000000000}}
        for identifier in (str(oid), cloid):
            result, code = self.read("order-status", id=identifier)
            self.assertEqual(code, 200)
            self.assertTrue(result["found"])
            self.assertEqual(result["order_id"], str(oid))
            self.assertEqual(result["remainingSize"], 1.5)
            self.assertEqual(result["remainingSizeExact"], "1.5")
            self.assertEqual(result["originalSizeExact"], "2")
            self.assertEqual(result["side"], "buy")
            self.assertTrue(result["reduceOnly"])
            self.assertEqual(result["orderStatus"], "open")
            self.assertNotIn("filledSize", result)
        self.assertEqual(self.calls[-1][1]["oid"], cloid)
        self.data["orderStatus"] = {"status": "unknownOid"}
        result, code = self.read("order-status", id=str(oid))
        self.assertEqual(code, 200)
        self.assertFalse(result["found"])
        self.assertTrue(result["uncertain"])
        self.data["orderStatus"] = {"status": "order", "order": {
            "order": {**order, "oid": 7}, "status": "filled", "statusTimestamp": 1700000000000}}
        result, code = self.read("order-status", id=str(oid))
        self.assertEqual(code, 503)
        self.assertEqual(result["state"], "unavailable")
        self.assertIn("identity mismatch", result["error"])

    def test_fills_keep_exact_order_identity_and_exchange_accounting(self):
        result, code = self.read("fills")
        self.assertEqual(code, 200)
        fill = result["fills"][0]
        self.assertEqual(fill["order_id"], "12345678901234567890")
        self.assertEqual(fill["id"], "88")
        self.assertEqual((fill["fee"], fill["feeToken"], fill["realizedPnl"]), (0.01, "USDC", 0.2))
        self.assertEqual(fill["fillType"], "taker", "crossed = took liquidity")
        self.assertEqual(result["history"], "recent-only", "recent fills are not complete history")

    def test_order_status_invalid_ids_never_reach_upstream(self):
        for identifier in (None, "", "0", "-1", "1e6", str(2**64), "0xbad"):
            with self.subTest(identifier=identifier):
                result, code = self.read("order-status", id=identifier)
                self.assertEqual(code, 503)
                self.assertEqual(result["state"], "unavailable")
        self.assertEqual(self.calls, [])

    def test_close_uses_fresh_exact_exposure_and_rejects_flat_or_changed_positions(self):
        self.assertEqual(self.backend.positions()["positions"][0]["sizeExact"], "2")
        self.backend.validate_close("HL_APT", "buy", "2")
        with self.assertRaises(HyperliquidError):
            self.backend.validate_close("HL_APT", "buy", "2.0000000000000001")
        position = self.data["clearinghouseState"]["assetPositions"][0]["position"]
        position["szi"] = "-1"
        with self.assertRaisesRegex(HyperliquidError, "exceeds"):
            self.backend.validate_close("HL_APT", "buy", "2")
        position["szi"] = "2"
        with self.assertRaises(HyperliquidError):
            self.backend.validate_close("HL_APT", "buy", "2")
        self.backend.validate_close("HL_APT", "sell", "1")
        position["szi"] = "0"
        with self.assertRaises(HyperliquidError):
            self.backend.validate_close("HL_APT", "sell", "1")
        self.data["clearinghouseState"].pop("assetPositions")
        with self.assertRaises(HyperliquidError):
            self.backend.validate_close("HL_APT", "sell", "1")

    def test_api_agent_binding_is_fresh_and_fails_closed(self):
        from unittest.mock import patch
        signer = "0x" + "a" * 40
        approved = {"address": signer.upper().replace("0X", "0x"), "validUntil": 1000001}
        with patch("hyperliquid_backend.time.time", return_value=1000):
            self.data["extraAgents"] = [approved]
            self.backend.require_agent(signer)
            self.assertEqual(self.calls[-1], ("extraAgents", {"user": self.backend.account_address}))
            for response in ([], [dict(approved, validUntil=1000000)], [dict(approved, validUntil="1000001")],
                             [approved, approved], {"error": "bad schema"}):
                self.data["extraAgents"] = response
                with self.subTest(response=response), self.assertRaises(HyperliquidError):
                    self.backend.require_agent(signer)
        self.assertEqual(len(self.calls), 6, "never reuse an old approval after revocation")

    def test_venue_health_does_not_assert_process_arm_state(self):
        # ARM belongs to the process. A hardcoded value here once made the UI show
        # DISARMED while the terminal was armed and writes were live.
        health = self.backend.health()
        self.assertNotIn("armed", health)
        self.assertNotIn("readOnly", health)
        self.assertTrue(health["ok"])

    def test_native_namespaces_precision_and_delisted_assets(self):
        payload, status = self.read("instruments")
        self.assertEqual(status, 200)
        btc, apt, old = payload["instruments"]
        self.assertEqual((btc["symbol"], btc["tickSize"]), ("HL_BTC", 1))
        self.assertEqual((apt["symbol"], apt["coin"], apt["assetId"], apt["contractSize"]), ("HL_APT", "APT", 1, 1))
        self.assertEqual(apt["tickSize"], .0001)
        self.assertFalse(old["tradeable"])
        markets, _ = self.read("marketlist")
        self.assertEqual(len(markets["rows"]), 2)
        self.assertTrue(all(row["priceBasis"] == "mark" and row["last"] is None for row in markets["rows"]))
        before = len(self.calls)
        _, status = self.read("orderbook", symbol="PF_APTUSD")
        self.assertEqual(status, 503)
        self.assertEqual(len(self.calls), before, "wrong-venue symbols must not cause an upstream query")

    def test_last_requires_real_trades_and_survives_mark_only_updates(self):
        result, _ = self.read("tickers", symbols="HL_APT")
        ticker = result["tickers"]["HL_APT"]
        self.assertEqual((ticker["last"], ticker["markPrice"]), (.6, .7))
        self.backend.on_message({"channel": "activeAssetCtx", "data": {"coin": "APT", "ctx": {
            **self.data["metaAndAssetCtxs"][1][1], "markPx": "0.75"}}})
        result, _ = self.read("tickers", symbols="HL_APT")
        self.assertEqual((result["tickers"]["HL_APT"]["last"], result["tickers"]["HL_APT"]["markPrice"]), (.6, .75))
        self.backend.on_message({"channel": "trades", "data": [{**self.data["recentTrades"][0], "px": "0.64", "time": 1700000001000}]})
        result, _ = self.read("tickers", symbols="HL_APT")
        self.assertEqual(result["tickers"]["HL_APT"]["last"], .64)
        self.backend.on_message({"channel": "trades", "data": self.data["recentTrades"]})
        result, _ = self.read("tickers", symbols="HL_APT")
        self.assertEqual(result["tickers"]["HL_APT"]["last"], .64, "older snapshots cannot roll last backward")

    def test_same_millisecond_trades_use_numeric_ids_and_ignore_replays(self):
        trade = self.data["recentTrades"][0]
        self.data["recentTrades"] = [{**trade, "tid": 100, "px": "0.61"}, {**trade, "tid": 99}]
        result, _ = self.read("tickers", symbols="HL_APT")
        self.assertEqual(result["tickers"]["HL_APT"]["last"], .61)
        self.backend.on_message({"channel": "trades", "data": [{**trade, "tid": 99, "px": "0.59"}]})
        result, _ = self.read("tickers", symbols="HL_APT")
        self.assertEqual(result["tickers"]["HL_APT"]["last"], .61)
        self.backend._cache.clear()
        self.data["recentTrades"] = [{**trade, "tid": None}]
        result, status = self.read("tickers", symbols="HL_APT")
        self.assertEqual((result["state"], status), ("unavailable", 503))

    def test_native_candles_and_book_never_use_binance_or_kraken(self):
        candles, status = self.read("candles", symbol="HL_APT", res="1m")
        self.assertEqual((status, candles["source"], candles["candles"][0]), (200, "hyperliquid", [1700000040, .6, .7, .5, .65, 42]))
        book, status = self.read("orderbook", symbol="HL_APT")
        self.assertEqual((status, book["orderBook"]["bids"][0][0], book["orderBook"]["asks"][0][0]), (200, .59, .60))
        self.backend.on_message({"channel": "candle", "data": self.data["candleSnapshot"][0]})
        kind, bar = self.publish.call_args.args
        self.assertEqual((kind, bar["symbol"], bar["source"], bar["v"]), ("bcandle", "HL_APT", "hyperliquid", 42))
        self.backend._cache.clear()
        self.data["l2Book"]["coin"] = "BTC"
        failed, status = self.read("orderbook", symbol="HL_APT")
        self.assertEqual((status, failed["state"]), (503, "unavailable"))
        self.assertEqual(failed["orderBook"], book["orderBook"], "keep only this venue's last known book")

    def test_empty_duplicate_or_wrong_symbol_candles_are_unavailable(self):
        candle = deepcopy(self.data["candleSnapshot"][0])
        for data in ([], [candle, candle], [{**candle, "s": "BTC"}], [{**candle, "c": "NaN"}], [{**candle, "l": "5"}]):
            self.backend._cache.clear()
            self.data["candleSnapshot"] = data
            result, status = self.read("candles", symbol="HL_APT", res="1m")
            self.assertEqual((status, result["state"]), (503, "unavailable"))

    def test_missing_wallet_is_unavailable_not_empty_or_zero(self):
        backend = HyperliquidBackend(Mock(), client=self.client, account_address="", enable_feed=False)
        for path in ("account", "positions", "orders", "fills"):
            result, _ = backend.read("/api/" + path, {})
            self.assertEqual(result["state"], "unavailable")
            self.assertNotIn("balanceValue", result)
            self.assertNotIn(path, result)
        self.client.info.assert_not_called()
        self.assertFalse(backend.health()["accountConfigured"])

    def test_unified_account_reports_one_balance_not_an_empty_perp(self):
        # Docs: in unified mode the perp dex state is not meaningful; the single USDC
        # balance is the collateral. Showing the perp zero would look like an empty account.
        self.data["userAbstraction"] = "unifiedAccount"
        self.data["spotClearinghouseState"] = {"balances": [{"coin": "USDC", "total": "13.77"}]}
        account, _ = self.read("account")
        self.assertTrue(account["unified"])
        self.assertEqual(account["balanceBasis"], "unified")
        self.assertEqual(account["balanceValue"], 13.77)
        self.assertIsNone(account["withdrawable"], "the perp withdrawable figure is meaningless here")
        for mode in ("portfolioMargin", "unifiedAccount"):
            self.data["userAbstraction"] = mode
            self.backend._cache.clear()
            self.assertTrue(self.read("account")[0]["unified"])

    def test_standard_account_keeps_spot_and_perp_apart(self):
        for mode in ("manual", "default", "dexAbstraction", None, 42):
            with self.subTest(mode=mode):
                self.data["userAbstraction"] = mode
                self.data["spotClearinghouseState"] = {"balances": [{"coin": "USDC", "total": "13.77"}]}
                self.backend._cache.clear()
                account, _ = self.read("account")
                self.assertFalse(account["unified"])
                self.assertEqual(account["balanceBasis"], "perp")
                self.assertEqual(account["balanceValue"], 100, "perp balance, not spot")
                self.assertEqual(account["spotUsdc"], 13.77)
                self.assertEqual(account["withdrawable"], 80)

    def test_unified_balance_stays_unknown_when_the_spot_read_fails(self):
        self.data["userAbstraction"] = "unifiedAccount"
        self.data["spotClearinghouseState"] = {"balances": [{"coin": "USDC", "total": None}]}
        account, _ = self.read("account")
        self.assertTrue(account["unified"])
        self.assertIsNone(account["balanceValue"], "unknown, never an invented 0")

    def test_account_reports_spot_and_perp_separately(self):
        self.data["spotClearinghouseState"] = {"balances": [{"coin": "USDC", "total": "13.77"},
                                                          {"coin": "PURR", "total": "0"}]}
        account, _ = self.read("account")
        self.assertEqual(account["mode"], "manual")
        self.assertEqual(account["balanceValue"], 100, "perp balance is not the spot balance")
        self.assertEqual(account["spotUsdc"], 13.77)
        self.assertEqual(account["balances"], {"USDC": 13.77, "PURR": 0.0})
        self.assertEqual(account["balanceBasis"], "perp")
        self.assertIsNone(account["availableMargin"])

    def test_spot_balance_distinguishes_known_zero_from_unreadable(self):
        known, _ = self.read("account")
        self.assertEqual(known["spotUsdc"], 0.0, "a successful read with no USDC is a known zero")
        self.assertEqual(known["state"], "current")
        self.backend._cache.clear()
        self.data["spotClearinghouseState"] = {"balances": [{"coin": "USDC", "total": None}]}
        failed, _ = self.read("account")
        self.assertIsNone(failed["spotUsdc"], "an unreadable spot balance stays unknown, never 0")
        self.assertEqual(failed["balances"], {})
        self.assertEqual(failed["balanceValue"], 100, "a spot failure must not hide the perp balance")
        self.assertEqual(failed["state"], "current")

    def test_account_queries_master_address_and_does_not_invent_available_margin(self):
        account, _ = self.read("account")
        self.assertEqual((account["balanceValue"], account["portfolioValue"], account["withdrawable"]), (100, 110, 80))
        self.assertIsNone(account["availableMargin"])
        positions, _ = self.read("positions")
        position = positions["positions"][0]
        self.assertEqual((position["symbol"], position["side"], position["size"], position["price"]), ("HL_APT", "short", 2, .8))
        self.assertEqual(position["liqPriceEstimate"], 1.2)
        self.assertNotIn("unrealizedFunding", position, "cumulative funding is not Kraken's unsettled funding")
        self.assertEqual([params for kind, params in self.calls if kind == "clearinghouseState"], [{"user": "0x" + "1" * 40}])

    def test_orders_keep_exact_large_ids_and_remaining_size(self):
        payload, _ = self.read("orders")
        order = payload["orders"][0]
        self.assertEqual(order["order_id"], "12345678901234567890")
        self.assertEqual((order["size"], order["unfilledSize"], order["orderType"]), (2, 1.5, "take_profit"))
        self.assertNotIn("filledSize", order)
        self.assertTrue(order["reduceOnly"])
        fills, _ = self.read("fills")
        self.assertEqual((fills["fills"][0]["symbol"], fills["fills"][0]["side"]), ("HL_APT", "sell"))

    def test_bad_private_response_preserves_this_venues_last_known_data(self):
        good, _ = self.read("positions")
        self.backend._cache.clear()
        self.data["clearinghouseState"]["assetPositions"] = [{"position": {"coin": "APT", "szi": None}}]
        failed, _ = self.read("positions")
        self.assertEqual(failed["state"], "unavailable")
        self.assertEqual(failed["positions"], good["positions"])
        self.assertGreaterEqual(failed["ageSeconds"], 0)
        self.backend._cache.clear()
        self.data["clearinghouseState"]["assetPositions"] = []
        flat, _ = self.read("positions")
        self.assertEqual((flat["state"], flat["positions"]), ("current", []))

    def test_catalog_mismatch_and_invalid_network_fail_closed(self):
        self.data["metaAndAssetCtxs"][1].pop()
        failed, status = self.read("instruments")
        self.assertEqual((failed["state"], status), ("unavailable", 503))
        invalid = HyperliquidBackend(Mock(), network="wrong", account_address="", enable_feed=False)
        self.assertFalse(invalid.health()["ok"])
        self.assertEqual(invalid.read("/api/instruments", {})[1], 503)

    def test_bare_string_info_response_is_accepted_but_other_shapes_are_not(self):
        transport = SimpleNamespace(post=Mock(return_value="unifiedAccount"))
        client = HyperliquidClient("mainnet", transport=transport)
        self.assertEqual(client.info("userAbstraction", user="0x" + "1" * 40), "unifiedAccount")
        for body in (123, None, True):
            with self.subTest(body=body):
                client = HyperliquidClient("mainnet", transport=SimpleNamespace(post=Mock(return_value=body)))
                with self.assertRaises(HyperliquidError):
                    client.info("userAbstraction", user="0x" + "1" * 40)
        client = HyperliquidClient("mainnet", transport=SimpleNamespace(post=Mock(return_value={"error": "nope"})))
        with self.assertRaises(HyperliquidError):
            client.info("userAbstraction", user="0x" + "1" * 40)

    def test_info_transport_is_read_only_and_rate_limit_is_not_retried(self):
        transport = SimpleNamespace(post=Mock(return_value=[]))
        client = HyperliquidClient("testnet", transport=transport)
        self.assertEqual(client.info("recentTrades", coin="APT"), [])
        transport.post.assert_called_once_with("/info", {"type": "recentTrades", "coin": "APT"})
        self.assertEqual(client.host, "api.hyperliquid-testnet.xyz")
        for kind in ("order", "cancel", "withdraw3", "approveAgent", "exchange"):
            with self.assertRaises(HyperliquidError):
                client.info(kind)
        self.assertEqual(transport.post.call_count, 1)
        transport.post.side_effect = ClientError(429, None, "rate limited", {"Retry-After": "120"})
        with self.assertRaises(HyperliquidError):
            client.info("recentTrades", coin="APT")
        with self.assertRaisesRegex(HyperliquidError, "cooldown"):
            client.info("recentTrades", coin="APT")
        self.assertEqual(transport.post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
