"""Official SDK integration, published keys and a frozen pre-migration vector.

All keys are public fixtures. No server imports, network or real credentials.
"""
from copy import deepcopy
import unittest
from unittest.mock import patch

from eth_account import Account
from eth_keys import keys
from eth_keys.constants import SECPK1_N
from hyperliquid.utils import signing as sdk

from hyperliquid_signing import SigningError, address_from_private_key, private_key_from_hex, sign_l1_action

KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ADDRESS = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
NONCE = 1789114000000


class SDKSigningTests(unittest.TestCase):
    def setUp(self):
        self.action = {"type": "cancel", "cancels": [{"a": 1, "o": 5}]}

    def test_published_key_addresses_and_leading_zeros(self):
        self.assertEqual(address_from_private_key(KEY), ADDRESS)
        self.assertEqual(address_from_private_key("0x" + "46" * 32), "0x9d8a62f656a8d1615c1294fd71e9cfb3e4855a4f")
        self.assertEqual(private_key_from_hex("0X" + "00" * 31 + "01"), bytes.fromhex("01".zfill(64)))
        self.assertEqual(address_from_private_key("01".zfill(64)), Account.from_key(bytes.fromhex("01".zfill(64))).address.lower())

    def test_sdk_crypto_matches_published_eip155_signature(self):
        digest = bytes.fromhex("daf5a779ae972f972197303d7b574746c7ef83eadac0f2791ad23db92e4c8e53")
        signature = keys.PrivateKey(bytes.fromhex("46" * 32)).sign_msg_hash(digest)
        self.assertEqual(hex(signature.r), "0x28ef61340bd939bc2195fe537567866003e1a15d3c71ff63e1590620aa636276")
        self.assertEqual(hex(signature.s), "0x67cbe9d8997f761aecb703304b3800ccf555c9f3dc64214b297fb1966a3b6d83")
        self.assertEqual(signature.v, 0)

    def test_frozen_pre_migration_cancel_vector(self):
        signature = sign_l1_action(self.action, KEY, NONCE, "mainnet")
        self.assertEqual(signature, {
            "r": "0x63334635e42cce01e60fe70847e109448c2ecdf70b41fc09b87336e01b78a994",
            "s": "0x38ab4077a3451996d61f6f0b82c2f7c344bb59eaf052706dc84552119cd92265", "v": 28})

    def test_delegates_exact_prepared_action_to_official_sdk(self):
        original = deepcopy(self.action)
        with patch("hyperliquid_signing.sdk_signing.sign_l1_action", wraps=sdk.sign_l1_action) as signing:
            result = sign_l1_action(self.action, KEY, NONCE, "testnet")
        signing.assert_called_once()
        args = signing.call_args.args
        self.assertEqual(args[0].address.lower(), ADDRESS)
        self.assertIs(args[1], self.action)
        self.assertEqual(args[2:], (None, NONCE, None, False))
        self.assertEqual(self.action, original)
        self.assertEqual(sdk.recover_agent_or_user_from_l1_action(self.action, result, None, NONCE, None, False).lower(), ADDRESS)

    def test_network_nonce_vault_expiry_and_wire_order_remain_bound(self):
        baseline = sign_l1_action(self.action, KEY, NONCE, "mainnet")
        for patch_args in ({"network": "testnet"}, {"nonce": NONCE + 1},
                           {"vault_address": "0x" + "ab" * 20}, {"expires_after": NONCE + 1000}):
            args = {"nonce": NONCE, "network": "mainnet", **patch_args}
            signature = sign_l1_action(self.action, KEY, **args)
            self.assertNotEqual(signature, baseline)
            self.assertEqual(sdk.recover_agent_or_user_from_l1_action(self.action, signature, args.get("vault_address"),
                args["nonce"], args.get("expires_after"), args["network"] == "mainnet").lower(), ADDRESS)
        reordered = {"cancels": self.action["cancels"], "type": "cancel"}
        self.assertNotEqual(sign_l1_action(reordered, KEY, NONCE), baseline)

    def test_supported_prepared_actions_recover_to_public_fixture(self):
        order = {"a": 1, "b": True, "p": "0.63", "s": "20", "r": False,
                 "t": {"limit": {"tif": "Ioc"}}, "c": "0x" + "a" * 32}
        actions = [{"type": "order", "orders": [order], "grouping": "na"},
                   {"type": "order", "orders": [order, {**order, "c": "0x" + "b" * 32}], "grouping": "na"},
                   {"type": "cancelByCloid", "cancels": [{"asset": 1, "cloid": order["c"]}]},
                   {"type": "modify", "oid": 5, "order": order},
                   {"type": "updateLeverage", "asset": 1, "isCross": True, "leverage": 3}]
        for action in actions:
            for network in ("mainnet", "testnet"):
                before = deepcopy(action)
                signature = sign_l1_action(action, KEY, NONCE, network)
                self.assertEqual(action, before)
                self.assertEqual(sdk.recover_agent_or_user_from_l1_action(action, signature, None, NONCE, None, network == "mainnet").lower(), ADDRESS)
                self.assertLessEqual(int(signature["s"], 16), SECPK1_N // 2)

    def test_invalid_keys_nonce_network_and_vault_fail_before_sdk(self):
        for bad in ("", "0x", "zz" * 32, "11" * 31, "11" * 33, "00" * 32, hex(SECPK1_N)[2:], 42, None):
            with self.assertRaises(SigningError):
                private_key_from_hex(bad)
        with patch("hyperliquid_signing.sdk_signing.sign_l1_action") as signing:
            for nonce in (0, -1, 2**64, True, 1.5, "1", None):
                with self.assertRaises(SigningError):
                    sign_l1_action(self.action, KEY, nonce)
            for options in ({"network": "wrong"}, {"vault_address": "0x123"}, {"expires_after": True}, {"expires_after": 0}):
                with self.assertRaises(SigningError):
                    sign_l1_action(self.action, KEY, NONCE, **options)
            signing.assert_not_called()

    def test_sdk_failure_is_sanitized_without_crypto_fallback(self):
        with patch("hyperliquid_signing.sdk_signing.sign_l1_action", side_effect=ValueError("sensitive payload")) as signing:
            with self.assertRaisesRegex(SigningError, "Official SDK") as caught:
                sign_l1_action(self.action, KEY, NONCE)
            self.assertNotIn("sensitive payload", str(caught.exception))
            signing.assert_called_once()


if __name__ == "__main__":
    unittest.main()
