import ast
import json
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from db import Database
from hyperliquid_client import HyperliquidError
import hyperliquid_chart as chart
import hyperliquid_trading as trading
import hyperliquid_recovery as recovery
from test_hyperliquid_trading import make_trader, server_write_helpers, ACCOUNT


class Backend:
    network = 'testnet'
    account_address = ACCOUNT
    account_configured = True
    def __init__(self):
        self.position = {'symbol': 'HL_APT', 'side': 'long', 'sizeExact': '10', 'price': 1}
        self.open = []
        self.require_agent = Mock()
        self.order_status = Mock()
    def markets(self):
        return {'HL_APT': {'instrument': {'assetId': 1, 'contractValueTradePrecision': 2, 'tradeable': True}}}
    def positions(self, *, fresh=False):
        assert fresh
        return {'positions': [deepcopy(self.position)]}
    def orders(self, *, fresh=False):
        assert fresh
        return {'orders': deepcopy(self.open)}
    def quote(self, symbol):
        return self.orderbook(symbol, fresh=True)

    def orderbook(self, symbol, *, fresh=False):
        assert fresh
        return {'time': int(time.time()*1000), 'orderBook': {'bids': [[0.99, 100]], 'asks': [[1.01, 100]]}}


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        # Trigger mechanics are exercised through the stop loss; the TP is a maker limit
        # (see MakerTakeProfitTests).
        self.body = {'symbol': 'HL_APT', 'kind': 'sl', 'price': 0.8, 'position': deepcopy(self.backend.position),
                     'cloid': '0x' + 'a'*32, 'expectedArmed': False}
    def target(self, market=True):
        row = {'order_id': str(2**63+5), 'symbol': 'HL_APT', 'side': 'sell', 'orderType': 'stp',
               'cliOrdId': '0x'+'b'*32, 'reduceOnly': True, 'triggerKind': 'sl', 'triggerMarket': market,
               'unfilledSizeExact': '3', 'stopPrice': 0.9, 'limitPrice': 0.89}
        self.backend.open = [row]
        self.body.update(target=deepcopy(row), acknowledgeReplacement=True)
        return row
    def test_orders_fake_matches_real_adapter_and_filters_other_symbols(self):
        import inspect
        from hyperliquid_backend import HyperliquidBackend
        self.assertEqual(str(inspect.signature(Backend.orders)), str(inspect.signature(HyperliquidBackend.orders)))
        self.backend.open = [{'symbol': 'HL_BTC', 'order_id': '900', 'orderType': 'take_profit', 'reduceOnly': True}]
        self.assertEqual(chart.prepare(self.body, self.backend)['action']['type'], 'order')

    def test_full_position_create_long_and_short_preserves_reduce_only(self):
        for side, kind, price in [('long','tp',1.2), ('long','sl',0.8), ('short','tp',0.8), ('short','sl',1.2)]:
            self.backend.position['side'] = side
            self.body.update(kind=kind, price=price, position=deepcopy(self.backend.position))
            intent = chart.prepare(self.body, self.backend)
            order = intent['action']['orders'][0]
            self.assertTrue(order['r'])
            self.assertEqual(order['s'], '10')
            self.assertEqual(order['b'], side == 'short')
            if kind == 'tp':
                self.assertEqual(order['t'], {'limit': {'tif': 'Alo'}}, 'a TP is a post-only maker limit')
            else:
                self.assertEqual(order['t']['trigger']['tpsl'], kind)
                self.assertTrue(order['t']['trigger']['isMarket'])
    def test_native_full_position_uses_zero_size_for_long_short_tp_sl(self):
        self.body.update(fullPosition=True, acknowledgeFullPosition=True)
        for side, kind, price in [('long','sl',0.8), ('short','sl',1.2)]:
            for size in ('10', '30', '5'):
                self.backend.position.update(side=side, sizeExact=size)
                self.body.update(kind=kind, price=price, position=deepcopy(self.backend.position))
                intent = chart.prepare(self.body, self.backend)
                self.assertTrue(intent['fullPosition'])
                self.assertEqual(intent['action']['grouping'], 'positionTpsl')
                wire = intent['action']['orders'][0]
                self.assertEqual(wire['s'], '0')
                self.assertTrue(wire['r'])
                self.assertEqual(wire['b'], side == 'short')
                self.assertEqual(wire['t']['trigger']['tpsl'], kind)
                chart.validate(intent, self.backend)
        self.body['acknowledgeFullPosition'] = False
        with self.assertRaisesRegex(HyperliquidError, 'acknowledged'): chart.prepare(self.body, self.backend)

    def test_conversion_and_drag_preserve_native_sizing_and_reject_partial_ladders(self):
        self.target(market=False)
        self.body.update(fullPosition=True, acknowledgeFullPosition=True)
        intent = chart.prepare(self.body, self.backend)
        wire = intent['action']['modifies'][0]['order']
        self.assertEqual(wire['s'], '0')
        self.assertEqual(wire['p'], '0.89')
        self.assertEqual(intent['action']['modifies'][0]['oid'], 2**63+5)
        self.backend.open.append({**self.backend.open[0], 'order_id': '123'})
        with self.assertRaisesRegex(HyperliquidError, 'ladder'): chart.prepare(self.body, self.backend)
        with self.assertRaisesRegex(HyperliquidError, 'ladder'): chart.validate(intent, self.backend)
        self.backend.open.pop()
        self.backend.open[0].update(positionTpsl=True, unfilledSizeExact='0')
        self.body['target'] = deepcopy(self.backend.open[0])
        self.body.pop('fullPosition')
        intent = chart.prepare(self.body, self.backend)
        self.assertEqual(intent['action']['modifies'][0]['order']['s'], '0')
        self.backend.position['sizeExact'] = '20'
        with self.assertRaisesRegex(HyperliquidError, 'Position changed'): chart.validate(intent, self.backend)
        self.body['position'] = deepcopy(self.backend.position)
        self.assertEqual(chart.prepare(self.body, self.backend)['action']['modifies'][0]['order']['s'], '0')

    def test_exact_trigger_modify_preserves_partial_ladder_and_limit_type(self):
        row = self.target(market=False)
        sibling = {**row, 'order_id': '123', 'unfilledSizeExact': '2'}
        self.backend.open.append(sibling)
        intent = chart.prepare(self.body, self.backend)
        action = intent['action']
        self.assertEqual(action['type'], 'batchModify')
        self.assertTrue(action['a'])
        self.assertEqual(len(action['modifies']), 1)
        self.assertEqual(action['modifies'][0]['oid'], 2**63+5)
        order = action['modifies'][0]['order']
        self.assertEqual(order['s'], '3')
        self.assertEqual(order['p'], '0.89')
        self.assertFalse(order['t']['trigger']['isMarket'])
        self.assertEqual(self.backend.open[1], sibling)
        self.body['acknowledgeReplacement'] = False
        with self.assertRaises(HyperliquidError): chart.prepare(self.body, self.backend)
    def test_changed_disappeared_wrong_target_and_existing_protection_fail_closed(self):
        self.target()
        intent = chart.prepare(self.body, self.backend)
        self.backend.open[0]['unfilledSizeExact'] = '2'
        with self.assertRaises(HyperliquidError): chart.validate(intent, self.backend)
        self.backend.open.clear()
        with self.assertRaises(HyperliquidError): chart.validate(intent, self.backend)
        self.target()
        self.body.pop('target')
        with self.assertRaises(HyperliquidError): chart.prepare(self.body, self.backend)
        self.backend.open.clear()
        self.backend.position['side'] = 'short'
        with self.assertRaises(HyperliquidError): chart.prepare(self.body, self.backend)
    def test_asset_identity_change_and_expired_quote_prevent_submission(self):
        intent = chart.prepare(self.body, self.backend)
        with patch('hyperliquid_chart.time.time', return_value=(intent['quoteTime']+16000)/1000):
            with self.assertRaisesRegex(HyperliquidError, 'quote expired'): chart.validate(intent, self.backend)
        self.backend.markets = lambda: {'HL_APT': {'instrument': {'assetId': 2, 'tradeable': True}}}
        with self.assertRaisesRegex(HyperliquidError, 'Instrument identity'): chart.validate(intent, self.backend)

    def test_invalid_and_already_executable_prices_are_rejected(self):
        for price in (0, -1, float('nan'), 0.99):
            with self.assertRaises(HyperliquidError): chart.prepare({**self.body, 'price': price}, self.backend)
    def namespace(self, trader):
        ns = {'Any': Any, 'json': json, 'time': time, 'hyperliquid_trading': trading, 'hyperliquid_chart': chart,
              'hyperliquid_recovery': recovery, 'hyperliquid': self.backend, 'arm_lock': threading.RLock(),
              'armed': False, '_hl_trader': {'ready': True, 'trader': trader, 'reason': None}, '_hl_gate': {},
              'READ_ONLY_MESSAGE': 'disabled', 'HL_TIF': {}, 'HL_TRIGGER': {}}
        exec(compile(server_write_helpers(), 'isolated_chart_write', 'exec'), ns)
        return ns
    def test_simulation_arm_change_and_actual_signed_native_wire_with_fake_transport(self):
        self.target()
        trader = make_trader(response={'status':'ok','response':{'type':'order','data':{'statuses':[{'resting':{'oid':42}}]}}})
        ns = self.namespace(trader)
        intent = chart.prepare(self.body, self.backend)
        result = ns['hyperliquid_write']('/api/chart-order', self.body, prepared_action=intent)
        self.assertEqual(result['outcome'], 'simulated')
        self.assertEqual(trader.transport.payloads, [])
        ns['armed'] = True
        with self.assertRaisesRegex(HyperliquidError, 'ARM state'): ns['hyperliquid_write']('/api/chart-order', self.body, prepared_action=intent)
        self.body['expectedArmed'] = True
        result = ns['hyperliquid_write']('/api/chart-order', self.body, prepared_action=intent)
        self.assertEqual(result['outcome'], 'confirmed')
        payload = trader.transport.payloads[0]
        self.assertEqual(payload['action'], intent['action'])
        self.assertEqual(payload['expiresAfter'], intent['expiresAfter'])
        self.assertIn('signature', payload)
        self.backend.require_agent.assert_called_once()
    def test_native_full_position_signing_uses_sdk_and_same_expiry_without_retries(self):
        self.body.update(fullPosition=True, acknowledgeFullPosition=True, expectedArmed=True)
        trader = make_trader(response={'status':'ok','response':{'type':'order','data':{'statuses':[{'resting':{'oid':42}}]}}})
        ns = self.namespace(trader)
        ns['armed'] = True
        intent = chart.prepare(self.body, self.backend)
        result = ns['hyperliquid_write']('/api/chart-order', self.body, prepared_action=intent)
        self.assertEqual(result['outcome'], 'confirmed')
        payload = trader.transport.payloads[0]
        self.assertEqual(payload['action']['orders'][0]['s'], '0')
        self.assertEqual(payload['action']['grouping'], 'positionTpsl')
        self.assertEqual(payload['expiresAfter'], intent['expiresAfter'])
        self.assertIn('signature', payload)
        self.assertEqual(len(trader.transport.payloads), 1)

    def test_bare_ack_is_unknown_and_no_automatic_retry_occurs(self):
        self.target()
        intent = chart.prepare(self.body, self.backend)
        for reply in ({'status':'ok','response':{'type':'default'}}, None):
            trader = make_trader(response=reply, error=HyperliquidError('lost') if reply is None else None)
            result = trader.submit(intent['action'], expires_after=intent['expiresAfter'])
            self.assertEqual(result['outcome'], 'unknown')
            self.assertEqual(len(trader.transport.payloads), 1)
    def test_native_recovery_requires_full_position_flag_and_matching_trigger(self):
        self.target()
        self.body.update(fullPosition=True, acknowledgeFullPosition=True)
        intent = chart.prepare(self.body, self.backend)
        db = Mock()
        db.venue_recovery_state.return_value = {'result': {'outcome': 'unknown'}}
        status = {'found': True, 'cliOrdId': self.body['cloid'], 'symbol': 'HL_APT', 'reduceOnly': True,
                  'side': 'sell', 'originalSizeExact': '0', 'order_id': '42', 'orderStatus': 'open',
                  'positionTpsl': True, 'isTrigger': True, 'triggerPrice': 0.8}
        old = {'found': True, 'orderStatus': 'canceled', 'symbol': 'HL_APT', 'order_id': self.body['target']['order_id']}
        with patch('hyperliquid_chart.time.time', return_value=(intent['expiresAfter']+5000)/1000):
            for bad in ({'positionTpsl': False}, {'triggerPrice': 1.3}, {'isTrigger': False}, {'uncertain': True}):
                self.backend.order_status.side_effect = [{**status, **bad}, old]
                with self.assertRaisesRegex(HyperliquidError, 'identity'): chart.reconcile(db, self.backend, 'native-recovery', self.body, intent)
                db.save_write_reconciliation.assert_not_called()
            self.backend.order_status.side_effect = [status, old]
            self.assertEqual(chart.reconcile(db, self.backend, 'native-recovery', self.body, intent)['outcome'], 'reconciled')
            db.save_write_reconciliation.assert_called_once()

    def test_restart_barrier_and_expired_exact_replacement_readback(self):
        self.target()
        intent = chart.prepare(self.body, self.backend)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'fixture.db'
            db = Database(path)
            payload = {'venue':'hyperliquid','network':'testnet','account':ACCOUNT,'body':self.body}
            db.claim_write_request('chart-fixture', '/api/chart-order', payload)
            db.prepare_hyperliquid_order('chart-fixture', intent)
            original = {'outcome':'unknown','error':'lost'}
            db.complete_write_request('chart-fixture', 200, original)
            db._conn.close()
            db = Database(path)
            try:
                self.assertEqual(db.claim_write_request('other-order','/api/order',payload)['state'],'blocked')
                self.assertEqual(recovery.unresolved(db,self.backend)['items'][0]['kind'],'chart')
                with self.assertRaisesRegex(HyperliquidError, 'expire'): recovery.reconcile(db,self.backend,'chart-fixture')
                self.backend.order_status.side_effect = [
                    {'found':True,'cliOrdId':self.body['cloid'],'symbol':'HL_APT','reduceOnly':True,'side':'sell','originalSizeExact':'3','order_id':'42','orderStatus':'open'},
                    {'found':True,'orderStatus':'canceled','symbol':'HL_APT','order_id':self.body['target']['order_id']}]
                with patch('hyperliquid_chart.time.time',return_value=(intent['expiresAfter']+5000)/1000):
                    result = recovery.reconcile(db,self.backend,'chart-fixture')
                self.assertEqual(result['outcome'],'reconciled')
                self.assertFalse(result['canReplace'])
                self.assertEqual(db.venue_recovery_state('chart-fixture','testnet',ACCOUNT)['result'],original)
                self.assertEqual(db.claim_write_request('next-order','/api/order',payload)['state'],'new')
            finally: db._conn.close()



class MakerTakeProfitTests(unittest.TestCase):
    """The chart TP on Hyperliquid is a post-only reduce-only limit, sized to the position."""

    def setUp(self):
        self.backend = Backend()
        self.body = {'symbol': 'HL_APT', 'kind': 'tp', 'price': 1.2, 'position': deepcopy(self.backend.position),
                     'cloid': '0x' + 'a'*32, 'expectedArmed': False, 'fullPosition': True, 'acknowledgeFullPosition': True}

    def maker_tp(self, **changes):
        row = {'order_id': str(2**63+7), 'symbol': 'HL_APT', 'side': 'sell', 'orderType': 'lmt', 'cliOrdId': '0x'+'c'*32,
               'reduceOnly': True, 'triggerKind': None, 'triggerMarket': None, 'positionTpsl': False,
               'unfilledSizeExact': '10', 'stopPrice': None, 'limitPrice': 1.15}
        row.update(changes)
        return row

    def test_a_new_tp_is_an_alo_limit_for_the_whole_position_with_no_trigger(self):
        intent = chart.prepare(self.body, self.backend)
        order = intent['action']['orders'][0]
        self.assertEqual((order['t'], order['s'], order['p'], order['r'], order['b']), ({'limit': {'tif': 'Alo'}}, '10', '1.2', True, False))
        self.assertEqual(intent['action']['grouping'], 'na')
        self.assertTrue(intent['maker'])
        chart.validate(intent, self.backend)

    def test_the_tp_price_must_rest_on_the_exit_side_of_the_book(self):
        # Long: a sell must sit above the best bid (0.99). Between bid and ask it still rests.
        self.assertEqual(chart.prepare({**self.body, 'price': 1.0}, self.backend)['action']['orders'][0]['p'], '1')
        with self.assertRaisesRegex(HyperliquidError, 'above the best bid'):
            chart.prepare({**self.body, 'price': 0.99}, self.backend)
        self.backend.position['side'] = 'short'
        short = {**self.body, 'position': deepcopy(self.backend.position), 'price': 1.01}
        with self.assertRaisesRegex(HyperliquidError, 'below the best ask'):
            chart.prepare(short, self.backend)

    def test_an_existing_maker_tp_blocks_a_second_one_and_moves_by_modify(self):
        row = self.maker_tp()
        self.backend.open = [row]
        with self.assertRaisesRegex(HyperliquidError, 'already exists'):
            chart.prepare(self.body, self.backend)
        body = {**self.body, 'target': deepcopy(row), 'acknowledgeReplacement': True, 'price': 1.3}
        intent = chart.prepare(body, self.backend)
        modify = intent['action']
        self.assertEqual((modify['type'], modify['modifies'][0]['oid']), ('batchModify', 2**63+7))
        self.assertNotIn('a', modify, 'the trigger-only a=true flag is not sent for a plain limit')
        self.assertEqual(modify['modifies'][0]['order']['t'], {'limit': {'tif': 'Alo'}})
        self.assertEqual(modify['modifies'][0]['order']['p'], '1.3')

    def test_a_legacy_trigger_tp_is_not_converted_by_modify(self):
        trigger = {'order_id': str(2**63+5), 'symbol': 'HL_APT', 'side': 'sell', 'orderType': 'take_profit',
                   'cliOrdId': '0x'+'b'*32, 'reduceOnly': True, 'triggerKind': 'tp', 'triggerMarket': True,
                   'positionTpsl': True, 'unfilledSizeExact': '0', 'stopPrice': 1.1, 'limitPrice': 1.1}
        self.backend.open = [trigger]
        body = {**self.body, 'target': deepcopy(trigger), 'acknowledgeReplacement': True}
        with self.assertRaisesRegex(HyperliquidError, 'older market-trigger TP'):
            chart.prepare(body, self.backend)
        with self.assertRaisesRegex(HyperliquidError, 'already exists'):
            chart.prepare(self.body, self.backend)

    def test_a_chase_exit_is_not_a_take_profit(self):
        self.backend.open = [self.maker_tp(cliOrdId='0x63686173' + 'd'*24)]
        self.assertEqual(chart.prepare(self.body, self.backend)['action']['type'], 'order')

    def test_recovery_of_a_maker_tp_compares_size_and_never_reads_a_trigger(self):
        intent = chart.prepare(self.body, self.backend)
        db = Mock()
        db.venue_recovery_state.return_value = {'result': {'outcome': 'unknown'}}
        status = {'found': True, 'cliOrdId': self.body['cloid'], 'symbol': 'HL_APT', 'reduceOnly': True, 'side': 'sell',
                  'originalSizeExact': '10', 'order_id': '42', 'orderStatus': 'open', 'isTrigger': False}
        with patch('hyperliquid_chart.time.time', return_value=(intent['expiresAfter'] + 5000) / 1000):
            for bad in ({'originalSizeExact': '9'}, {'isTrigger': True}, {'side': 'buy'}):
                self.backend.order_status.side_effect = [{**status, **bad}]
                with self.assertRaisesRegex(HyperliquidError, 'identity'):
                    chart.reconcile(db, self.backend, 'maker-recovery', self.body, intent)
            self.backend.order_status.side_effect = [status]
            self.assertEqual(chart.reconcile(db, self.backend, 'maker-recovery', self.body, intent)['outcome'], 'reconciled')


if __name__ == '__main__': unittest.main()
