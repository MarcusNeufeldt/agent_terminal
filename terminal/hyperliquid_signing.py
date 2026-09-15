"""Validated bridge to Hyperliquid's official Python SDK. No network or key storage."""
import re

from eth_account import Account
from eth_keys.constants import SECPK1_N
from hyperliquid.utils import signing as sdk_signing


class SigningError(ValueError):
    pass


def private_key_from_hex(value):
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str):
        text = value[2:] if value.startswith(("0x", "0X")) else value
        if not re.fullmatch(r"[0-9a-fA-F]{64}", text):
            raise SigningError("Private key must contain exactly 32 hex bytes")
        raw = bytes.fromhex(text)
    else:
        raise SigningError("Private key must be hex text or bytes")
    if len(raw) != 32 or not 1 <= int.from_bytes(raw, "big") < SECPK1_N:
        raise SigningError("Private key is outside the secp256k1 range")
    return raw


def address_from_private_key(private_key):
    return Account.from_key(private_key_from_hex(private_key)).address.lower()


def sign_l1_action(action, private_key, nonce, network="mainnet", vault_address=None, expires_after=None):
    if network not in {"mainnet", "testnet"}:
        raise SigningError("Network must be mainnet or testnet")
    for label, value in (("Nonce", nonce), ("Expiry", expires_after)):
        if label == "Expiry" and value is None:
            continue
        if type(value) is not int or not 0 < value < 2**64:
            raise SigningError(f"{label} must be a positive 64-bit integer")
    if vault_address is not None and (not isinstance(vault_address, str) or
                                     not re.fullmatch(r"0x[0-9a-fA-F]{40}", vault_address)):
        raise SigningError("Vault address must be a 20-byte hex address")
    wallet = Account.from_key(private_key_from_hex(private_key))
    try:
        return sdk_signing.sign_l1_action(wallet, action, vault_address, nonce, expires_after, network == "mainnet")
    except Exception as exc:
        # Do not expose a wallet/key or arbitrary SDK exception payload to the UI.
        raise SigningError("Official SDK could not sign the prepared action") from exc
