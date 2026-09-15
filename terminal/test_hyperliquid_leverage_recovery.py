import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from db import Database
from hyperliquid_client import HyperliquidError
from hyperliquid_recovery import reconcile, unresolved


class LeverageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'fixture.db'
        self.db = Database(self.path)
        self.account = '0x' + '1' * 40
        self.body = {'symbol': 'HL_APT', 'leverage': 5, 'cross': True, 'expectedLeverage': 3, 'expectedArmed': True}
        self.payload = {'venue': 'hyperliquid', 'network': 'testnet', 'account': self.account, 'body': self.body}
        self.intent = {'type': 'leverageIntent', 'action': {'type': 'updateLeverage', 'asset': 1, 'isCross': True, 'leverage': 5},
                       'expiresAfter': int(time.time() * 1000) - 10000}
        self.db.claim_write_request('leverage-original', '/api/leverage', self.payload)
        self.db.prepare_hyperliquid_order('leverage-original', self.intent)
        self.original = {'outcome': 'unknown', 'error': 'Response lost'}
        self.db.complete_write_request('leverage-original', 200, self.original)
        self.backend = SimpleNamespace(network='testnet', account_address=self.account, account_configured=True,
            orderbook=Mock(return_value={'time': int(time.time() * 1000)}),
            trading_capacity=Mock(return_value={'symbol': 'HL_APT', 'leverage': {'value': 5, 'type': 'cross'}}),
            markets=Mock(return_value={'HL_APT': {'instrument': {'assetId': 1}}}))

    def tearDown(self):
        self.db._conn.close()
        self.temp.cleanup()

    def test_restart_retains_barrier_and_expired_matching_readback_releases_it(self):
        self.db._conn.close()
        self.db = Database(self.path)
        item = unresolved(self.db, self.backend)['items'][0]
        self.assertEqual(item['kind'], 'leverage')
        self.assertEqual(self.db.claim_write_request('blocked-order', '/api/order', self.payload)['state'], 'blocked')
        evidence = reconcile(self.db, self.backend, 'leverage-original')
        self.assertEqual(evidence['outcome'], 'reconciled')
        self.assertFalse(evidence['canReplace'])
        self.backend.orderbook.assert_called_once_with('HL_APT', fresh=True)
        self.assertEqual(self.db.claim_write_request('next-order', '/api/order', self.payload)['state'], 'new')
        self.assertEqual(self.db.venue_recovery_state('leverage-original', 'testnet', self.account)['result'], self.original)

    def test_mismatching_or_stale_readback_keeps_submission_blocked(self):
        self.backend.trading_capacity.return_value['leverage']['value'] = 3
        with self.assertRaisesRegex(HyperliquidError, 'not observed'):
            reconcile(self.db, self.backend, 'leverage-original')
        self.backend.orderbook.return_value['time'] = 1
        with self.assertRaisesRegex(HyperliquidError, 'Fresh exchange time'):
            reconcile(self.db, self.backend, 'leverage-original')
        self.assertEqual(self.db.claim_write_request('next-setting', '/api/leverage', self.payload)['state'], 'blocked')

    def test_observed_value_before_request_expiry_cannot_release_barrier(self):
        self.backend.orderbook.return_value['time'] = self.intent['expiresAfter'] - 500
        with patch('hyperliquid_recovery.time.time', return_value=(self.intent['expiresAfter'] - 1000) / 1000):
            with self.assertRaisesRegex(HyperliquidError, 'in flight'):
                reconcile(self.db, self.backend, 'leverage-original')
        self.backend.trading_capacity.assert_not_called()
        self.assertEqual(self.db.claim_write_request('next-order', '/api/order', self.payload)['state'], 'blocked')

    def test_wrong_account_and_known_simulation_never_use_exchange_readback(self):
        self.backend.account_address = '0x' + '2' * 40
        with self.assertRaises(HyperliquidError):
            reconcile(self.db, self.backend, 'leverage-original')
        self.backend.account_address = self.account
        self.db._conn.close()
        self.db = Database(Path(self.temp.name) / 'simulation.db')
        self.db.claim_write_request('leverage-original', '/api/leverage', self.payload)
        self.db.prepare_hyperliquid_order('leverage-original', self.intent)
        self.db.complete_write_request('leverage-original', 200, {'outcome': 'simulated'})
        with self.assertRaisesRegex(HyperliquidError, 'known outcome'):
            reconcile(self.db, self.backend, 'leverage-original')
        self.backend.orderbook.assert_not_called()


if __name__ == '__main__':
    unittest.main()
