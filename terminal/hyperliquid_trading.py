"""Signed Hyperliquid exchange actions. Separate from the read-only client on purpose.

hyperliquid_client.py promises no signer and no /exchange transport. That promise
stays true: this module owns the only code path that can move funds or orders.

Rails, in order of precedence:
  1. Trading is off unless HYPERLIQUID_TRADING names testnet or mainnet.
  2. Action types are allowlisted; anything else cannot be signed or sent.
  3. Nonces are strictly increasing per process, so a batch cannot self-collide.
  4. Every request reports a terminal-style outcome. A transport failure is
     "unknown", never a silent success.
"""

import json
import os
import re
import secrets
import threading
import time
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING, Decimal, InvalidOperation, localcontext
from hyperliquid.utils.error import ClientError, ServerError
from requests import RequestException
from hyperliquid_sdk_http import SDKTransport

from hyperliquid_client import ADDRESS_RE, HOSTS, HyperliquidError, number
from hyperliquid_signing import SigningError, address_from_private_key, private_key_from_hex, sign_l1_action

TRADING_MODES = ("off", "testnet", "mainnet")
# Only these can ever be signed. Deliberately excludes withdraw, transfer,
# approveAgent, and every other fund-moving action type.
ALLOWED_ACTIONS = {"order", "cancel", "cancelByCloid", "modify", "batchModify", "updateLeverage"}
MAX_SIGNIFICANT_FIGURES = 5
MAX_DECIMALS = 6  # perps; spot uses 8
CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")
TIFS = {"gtc": "Gtc", "ioc": "Ioc", "alo": "Alo"}


class TradingDisabledError(HyperliquidError):
    pass


class HyperliquidRejectedError(HyperliquidError):
    """The exchange definitively did not apply the action (4xx, or status: "err").

    Distinct from HyperliquidError, which means the outcome is genuinely unknown:
    a timeout or 5xx may have been applied even though the response was lost.
    """


# Diagnostic sentinel only. The protocol does not guarantee this order id is unused.
UNREACHABLE_OID = 2 ** 62 - 1


def _decimal(value, label):
    if isinstance(value, bool) or value is None:
        raise HyperliquidError(f"{label} must be a positive finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise HyperliquidError(f"{label} must be a positive finite number") from exc
    if not result.is_finite():
        raise HyperliquidError(f"{label} must be a positive finite number")
    return result


def decimals_for(sz_decimals, exponent):
    """Significant-figure cap and decimal cap, whichever is stricter."""
    return max(0, min(MAX_DECIMALS - sz_decimals, MAX_SIGNIFICANT_FIGURES - 1 - exponent))


def format_price(value, sz_decimals, *, rounding=ROUND_HALF_UP):
    """Prices allow 5 significant figures and at most 6 - szDecimals decimals."""
    if type(sz_decimals) is not int or not 0 <= sz_decimals <= 6:
        raise HyperliquidError("Invalid size precision for this asset")
    price = _decimal(value, "Price")
    if price <= 0:
        raise HyperliquidError("Price must be a positive finite number")
    exponent = price.adjusted()
    try:
        rounded = price.quantize(Decimal(1).scaleb(-decimals_for(sz_decimals, exponent)), rounding=rounding)
    except InvalidOperation as exc:
        raise HyperliquidError("Price exceeds supported precision") from exc
    if rounded <= 0:
        raise HyperliquidError("Price rounds to zero at this asset's precision")
    return format(rounded.normalize(), "f")


def format_size(value, sz_decimals):
    """Sizes round down to szDecimals so a request never exceeds its intent."""
    size = _decimal(value, "Size")
    if size <= 0:
        raise HyperliquidError("Size must be a positive finite number")
    if type(sz_decimals) is not int or not 0 <= sz_decimals <= 6:
        raise HyperliquidError("Invalid size precision for this asset")
    try:
        rounded = size.quantize(Decimal(1).scaleb(-sz_decimals), rounding=ROUND_DOWN)
    except InvalidOperation as exc:
        raise HyperliquidError("Size exceeds supported precision") from exc
    if rounded <= 0:
        raise HyperliquidError(f"Size rounds to zero at {sz_decimals} decimals")
    return format(rounded.normalize(), "f")


def validate_order_notional(action, maximum):
    """Check nominal value at the final limit price, excluding fees and funding."""
    budget = _decimal(maximum, "Maximum notional")
    if budget <= 0:
        raise HyperliquidError("Maximum notional must be positive")
    orders = action.get("orders")
    if action.get("type") != "order" or not isinstance(orders, list) or not orders:
        raise HyperliquidError("Notional check requires a prepared order")
    if any(order.get("t", {}).get("trigger", {}).get("isMarket") for order in orders):
        raise HyperliquidError("Notional budgets require price-bounded orders, not market triggers")
    with localcontext() as context:
        context.prec = 80
        notional = sum(Decimal(order["p"]) * Decimal(order["s"]) for order in orders)
    if notional > budget:
        raise HyperliquidError("Rounded order exceeds the USD notional budget. Use an exchange-precision price or reduce size.")


def client_order_id():
    """16-byte hex cloid, matching the exchange's documented format."""
    return "0x" + secrets.token_hex(16)


SUCCESS_STATES = {"resting", "filled", "ok"}


def build_order(action_asset, side, size, price, tif, reduce_only, *, trigger=None, cloid=None, grouping="na"):
    """Order wire format: a=asset b=isBuy p=price s=size r=reduceOnly t=type c=cloid."""
    if side not in {"buy", "sell"}:
        raise HyperliquidError("Side must be buy or sell")
    if tif not in TIFS:
        raise HyperliquidError("Time in force must be gtc, ioc, or alo")
    if type(reduce_only) is not bool:
        raise HyperliquidError("reduceOnly must be a boolean")
    if trigger is None:
        order_type = {"limit": {"tif": TIFS[tif]}}
    else:
        if trigger.get("kind") not in {"tp", "sl"}:
            raise HyperliquidError("Trigger kind must be tp or sl")
        if type(trigger.get("market")) is not bool:
            raise HyperliquidError("Trigger market flag must be a boolean")
        order_type = {"trigger": {"isMarket": trigger["market"], "triggerPx": str(trigger["triggerPx"]),
                                  "tpsl": trigger["kind"]}}
    order = {"a": action_asset, "b": side == "buy", "p": str(price), "s": str(size),
             "r": reduce_only, "t": order_type}
    if cloid is not None:
        if not CLOID_RE.fullmatch(cloid):
            raise HyperliquidError("Client order ID must be 0x plus 32 hex characters")
        order["c"] = cloid
    if grouping not in {"na", "normalTpsl", "positionTpsl"}:
        raise HyperliquidError("Invalid grouping")
    return {"type": "order", "orders": [order], "grouping": grouping}


def order_action_for(instrument, side, size, price, *, tif="gtc", reduce_only=False, trigger=None,
                     cloid=None, grouping="na"):
    """One construction path for placement and for DISARMED previews."""
    return build_order(instrument["assetId"], side,
                       format_size(size, instrument.get("contractValueTradePrecision")),
                       format_price(price, instrument.get("contractValueTradePrecision")),
                       tif, reduce_only, trigger=trigger, cloid=cloid, grouping=grouping)


def percent_size(available, percent, maximum, precision):
    if type(percent) is not int or not 1 <= percent <= 100:
        raise HyperliquidError("Size percentage must be an integer from 1 to 100")
    with localcontext() as context:
        context.prec = 80
        quantity = min(_decimal(maximum, "Size"), _decimal(available, "Trading capacity") * percent / 100)
        return format_size(quantity, precision)


def market_action_for(instrument, side, size, snapshot, slippage_percent, *, cloid=None, reduce_only=False, maximum=None):
    """Market execution is a fresh-quote IOC with a conservative slippage bound."""
    slip = _decimal(slippage_percent, "Slippage percent")
    if not Decimal("0.01") <= slip <= 5:
        raise HyperliquidError("Slippage must be between 0.01% and 5%")
    stamp = snapshot.get("time") if isinstance(snapshot, dict) else None
    if type(stamp) not in (int, float) or not -5000 <= time.time() * 1000 - stamp <= 15000:
        raise HyperliquidError("Market quote timestamp is missing, stale or ahead of the local clock")
    if side not in {"buy", "sell"}:
        raise HyperliquidError("Side must be buy or sell")
    try:
        book = snapshot["orderBook"]
        bid, ask = _decimal(book["bids"][0][0], "Bid"), _decimal(book["asks"][0][0], "Ask")
    except (KeyError, IndexError, TypeError) as exc:
        raise HyperliquidError("Fresh two-sided market quotes are required") from exc
    if not 0 < bid < ask:
        raise HyperliquidError("Market quote is empty or crossed")
    precision = instrument["contractValueTradePrecision"]
    with localcontext() as context:
        context.prec = 80
        bound = (ask if side == "buy" else bid) * (1 + (slip / 100 if side == "buy" else -slip / 100))
        price = format_price(bound, precision, rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING)
        quantity = _decimal(size, "Size")
        if maximum is not None:
            budget = _decimal(maximum, "Maximum notional")
            if budget <= 0:
                raise HyperliquidError("Maximum notional must be positive")
            quantity = min(quantity, budget / Decimal(price))
        action = order_action_for(instrument, side, quantity, price, tif="ioc", reduce_only=reduce_only, cloid=cloid)
    if maximum is not None:
        validate_order_notional(action, maximum)
    return action


def parse_exchange_response(payload):
    """Normalize {status, response:{type, data:{statuses[]}}} into per-action rows."""
    if not isinstance(payload, dict):
        raise HyperliquidError("Invalid exchange response")
    status = payload.get("status")
    if status == "err":
        detail = payload.get("response")
        raise HyperliquidRejectedError(
            f"Exchange rejected the action: {detail if isinstance(detail, str) else 'unknown error'}")
    if status != "ok":
        raise HyperliquidError("Exchange response has no status field")
    response = payload.get("response")
    if not isinstance(response, dict):
        raise HyperliquidError("Exchange response is missing its body")
    data = response.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("statuses"), list):
        if response.get("type") == "default" and data is None:
            return {"type": "default", "rows": []}
        raise HyperliquidError("Exchange response has no status rows")
    rows = []
    for entry in data["statuses"]:
        if isinstance(entry, str):
            # cancel/cancelByCloid answer with "success" strings
            rows.append({"state": "ok"} if entry == "success" else {"state": "error", "error": entry})
            continue
        if not isinstance(entry, dict):
            raise HyperliquidError("Invalid exchange status row")
        if sum(key in entry for key in ("error", "resting", "filled")) != 1:
            raise HyperliquidError("Ambiguous exchange status row")
        if "error" in entry:
            if not isinstance(entry["error"], str) or not entry["error"]:
                raise HyperliquidError("Invalid exchange rejection")
            rows.append({"state": "error", "error": entry["error"]})
        elif isinstance(entry.get("resting"), dict):
            oid = entry["resting"].get("oid")
            if type(oid) is not int or not 0 < oid < 2**64:
                raise HyperliquidError("Invalid resting order identity")
            rows.append({"state": "resting", "oid": oid})
        elif isinstance(entry.get("filled"), dict):
            filled = entry["filled"]
            oid = filled.get("oid")
            if type(oid) is not int or not 0 < oid < 2**64:
                raise HyperliquidError("Invalid filled order identity")
            if _decimal(filled.get("totalSz"), "fill size") <= 0 or _decimal(filled.get("avgPx"), "fill price") <= 0:
                raise HyperliquidError("Invalid filled size or price")
            rows.append({"state": "filled", "oid": oid,
                         "totalSize": filled["totalSz"], "averagePrice": filled["avgPx"]})
        else:
            rows.append({"state": "unknown", "error": "Unrecognized status row"})
    ids = [row["oid"] for row in rows if "oid" in row]
    if len(ids) != len(set(ids)):
        raise HyperliquidError("Duplicate exchange order identities")
    return {"type": str(response.get("type") or "default"), "rows": rows}


def classify(rows, action_count):
    """Same outcome vocabulary the Kraken path uses."""
    if not rows or len(rows) != action_count:
        return "unknown"
    states = [row["state"] for row in rows]
    if all(state == "error" for state in states):
        return "rejected"
    if all(state in SUCCESS_STATES for state in states):
        return "confirmed" if len(rows) >= action_count else "partial"
    if any(state in SUCCESS_STATES for state in states):
        return "partial"
    return "unknown"


class NonceSource:
    """Strictly increasing millisecond nonces. Reuse is a rejected action."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._last = 0

    def next(self):
        with self._lock:
            candidate = max(int(self._clock() * 1000), self._last + 1)
            self._last = candidate
            return candidate


class ExchangeTransport:
    """The only path that can POST /exchange."""

    def __init__(self, host, *, transport=None):
        if host not in HOSTS.values():
            raise HyperliquidError("Unsupported Hyperliquid exchange host")
        self.host = host
        self._transport = transport if transport is not None else SDKTransport(f"https://{host}", timeout=12, max_bytes=1024 * 1024)

    def post(self, payload):
        try:
            return self._transport.post("/exchange", payload)
        except ClientError as exc:
            raise HyperliquidRejectedError(f"Exchange rejected the request with HTTP {exc.status_code}") from exc
        except ServerError as exc:
            raise HyperliquidError(f"Exchange HTTP {exc.status_code}") from exc
        except (RequestException, OSError, ValueError) as exc:
            raise HyperliquidError(f"Exchange request failed: {type(exc).__name__}") from exc


class HyperliquidTrader:
    """Signs and submits order actions. Callers own the ARM gate and request IDs."""

    def __init__(self, market, *, transport, private_key, account_address, network="testnet", nonce=None):
        if network not in HOSTS:
            raise HyperliquidError("HYPERLIQUID_NETWORK must be mainnet or testnet")
        self.market = market
        self.transport = transport
        self.network = network
        self.key = private_key_from_hex(private_key)
        self.address = address_from_private_key(self.key)
        account = str(account_address or "").strip().lower()
        if not ADDRESS_RE.fullmatch(account) or int(account[2:], 16) == 0:
            raise HyperliquidError("HYPERLIQUID_ACCOUNT_ADDRESS must be the master or subaccount address")
        self.account_address = account
        self.nonce = nonce or NonceSource()

    def _instrument(self, symbol):
        instrument = self.market(symbol)
        if not isinstance(instrument, dict):
            raise HyperliquidError("Unknown Hyperliquid symbol")
        asset = instrument.get("assetId")
        decimals = instrument.get("contractValueTradePrecision")
        if type(asset) is not int or asset < 0 or type(decimals) is not int:
            raise HyperliquidError("Instrument is missing its asset id or size precision")
        return instrument

    def _submit(self, action, *, count=1, expires_after=None):
        if action.get("type") not in ALLOWED_ACTIONS:
            raise HyperliquidError(f"Refusing to sign unsupported action {action.get('type')!r}")
        if expires_after is not None and (type(expires_after) is not int or
                not time.time() * 1000 < expires_after <= time.time() * 1000 + 60000):
            raise HyperliquidError("Request expiry is invalid or elapsed")
        nonce = self.nonce.next()
        try:
            signature = sign_l1_action(action, self.key, nonce, self.network, expires_after=expires_after)
        except SigningError as exc:
            raise HyperliquidError(f"Could not sign the action: {exc}") from exc
        payload = {"action": action, "nonce": nonce, "signature": signature}
        if expires_after is not None:
            if time.time() * 1000 >= expires_after:
                raise HyperliquidError("Request expired before transmission")
            payload["expiresAfter"] = expires_after
        try:
            response = self.transport.post(payload)
        except HyperliquidRejectedError as exc:
            return {"outcome": "rejected", "error": str(exc), "action": action, "nonce": nonce, "rows": []}
        except HyperliquidError as exc:
            # A lost response may still have placed the order, so this is not a failure.
            return {"outcome": "unknown", "error": str(exc), "action": action, "nonce": nonce,
                    "rows": [], "uncertain": True}
        try:
            parsed = parse_exchange_response(response)
        except HyperliquidRejectedError as exc:
            return {"outcome": "rejected", "error": str(exc), "action": action, "nonce": nonce,
                    "rows": [], "response": response}
        except HyperliquidError:
            return {"outcome": "unknown", "error": "Exchange response could not be classified",
                    "action": action, "nonce": nonce, "rows": [], "response": response, "uncertain": True}
        expected_type = "cancel" if action["type"] in {"cancel", "cancelByCloid"} else "order"
        if action["type"] == "updateLeverage":
            outcome = "confirmed" if parsed["type"] == "default" and not parsed["rows"] else "unknown"
        elif parsed["type"] != expected_type or any(
                row["state"] not in ({"ok", "error"} if expected_type == "cancel"
                                     else {"resting", "filled", "error"})
                for row in parsed["rows"]):
            outcome = "unknown"
        else:
            outcome = classify(parsed["rows"], count)
        action_orders = (action.get("orders", []) if action["type"] == "order" else
                         [item["order"] for item in action.get("modifies", [])])
        if any(row["state"] == "filled" and _decimal(row["totalSize"], "fill size") > _decimal(order["s"], "order size")
               for row, order in zip(parsed["rows"], action_orders)):
            outcome = "unknown"
        if action["type"] == "batchModify" and any(row.get("oid") == item["oid"]
                for row, item in zip(parsed["rows"], action["modifies"])):
            outcome = "unknown"
        result = {"outcome": outcome, "action": action, "nonce": nonce,
                  "rows": parsed["rows"], "response": response}
        # Any rejection is reported with the exchange's exact message.
        errors = [row.get("error") for row in parsed["rows"] if row.get("error")]
        if errors:
            result["error"] = "; ".join(errors)
        elif outcome == "unknown":
            result["error"] = "Exchange returned an unrecognized status"
        if outcome == "unknown":
            result["uncertain"] = True
        return result

    def place(self, symbol, side, size, price, *, tif="gtc", reduce_only=False, trigger=None,
              cloid=None, grouping="na"):
        order = order_action_for(self._instrument(symbol), side, size, price, tif=tif,
                                 reduce_only=reduce_only, trigger=trigger, cloid=cloid, grouping=grouping)
        return self._submit(order, count=len(order["orders"]))

    def submit(self, action, count=1, *, expires_after=None):
        """Submit a pre-built action so previews and writes share one construction."""
        return self._submit(action, count=count, expires_after=expires_after)

    def cancel(self, oid):
        asset, identifier = self._identity(oid)
        return self._submit({"type": "cancel", "cancels": [{"a": asset, "o": identifier}]})

    def cancel_by_client_id(self, symbol, cloid):
        if not CLOID_RE.fullmatch(str(cloid or "")):
            raise HyperliquidError("Client order ID must be 0x plus 32 hex characters")
        return self._submit({"type": "cancelByCloid",
                             "cancels": [{"asset": self._instrument(symbol)["assetId"], "cloid": str(cloid)}]})

    def modify(self, oid, symbol, side, size, price, *, tif="gtc", reduce_only=False):
        instrument = self._instrument(symbol)
        _, identifier = self._identity(oid)
        order = order_action_for(instrument, side, size, price, tif=tif, reduce_only=reduce_only)["orders"][0]
        return self._submit({"type": "modify", "oid": identifier, "order": order})

    def set_leverage(self, symbol, leverage, *, cross=True):
        instrument = self._instrument(symbol)
        if type(leverage) is not int or not 1 <= leverage <= int(instrument.get("maxLeverage") or 0):
            raise HyperliquidError("Leverage is outside this asset's allowed range")
        return self._submit({"type": "updateLeverage", "asset": instrument["assetId"],
                             "isCross": bool(cross), "leverage": leverage})

    @staticmethod
    def _identity(oid):
        if isinstance(oid, dict):
            asset = oid.get("asset")
            raw = oid.get("oid")
            if type(asset) is not int or asset < 0 or type(raw) is not int or raw < 0:
                raise HyperliquidError("Order identity requires an asset index and order id")
            return asset, raw
        if type(oid) is not int or oid < 0:
            raise HyperliquidError("Order id must be a non-negative integer")
        raise HyperliquidError("Order identity requires an asset index; pass {asset, oid}")


def load_trading_credentials(environ=None):
    """Read the trading gate and credentials without starting anything."""
    env = os.environ if environ is None else environ
    mode = str(env.get("HYPERLIQUID_TRADING", "off")).strip().lower() or "off"
    if mode not in TRADING_MODES:
        return {"mode": "off", "reason": "HYPERLIQUID_TRADING must be off, testnet, or mainnet"}
    mode = "off" if mode == "off" else mode
    secret = str(env.get("HYPERLIQUID_SECRET_KEY", "")).strip()
    account = str(env.get("HYPERLIQUID_ACCOUNT_ADDRESS", "")).strip().lower()
    if mode == "off":
        return {"mode": "off", "reason": "Signed trading is disabled (HYPERLIQUID_TRADING=off)"}
    if not secret:
        return {"mode": "off", "reason": "HYPERLIQUID_SECRET_KEY is required for signed trading"}
    if not ADDRESS_RE.fullmatch(account):
        return {"mode": "off", "reason": "HYPERLIQUID_ACCOUNT_ADDRESS is required for signed trading"}
    try:
        # Validate the key before any transport is constructed.
        address = address_from_private_key(private_key_from_hex(secret))
    except SigningError as exc:
        return {"mode": "off", "reason": f"HYPERLIQUID_SECRET_KEY is invalid: {exc}"}
    return {"mode": mode, "private_key": secret, "account_address": account, "signer_address": address,
            "host": HOSTS["testnet" if mode == "testnet" else "mainnet"]}


def build_trader(market, *, environ=None, transport_factory=ExchangeTransport):
    """Return (trader, reason). Never raises for a disabled or misconfigured gate."""
    credentials = load_trading_credentials(environ)
    if credentials["mode"] == "off":
        return None, credentials["reason"]
    try:
        transport = transport_factory(credentials["host"])
        trader = HyperliquidTrader(market, transport=transport, private_key=credentials["private_key"],
                                   account_address=credentials["account_address"], network=credentials["mode"])
    except HyperliquidError as exc:
        return None, str(exc)
    if trader.address != credentials["account_address"]:
        # The API wallet signs for the account, so a mismatch is suspicious, not fatal.
        return trader, (f"API wallet {trader.address} signs for account {credentials['account_address']}")
    return trader, None


def check_signer_authorization(trader, symbol):
    """Confirm the exchange recognizes the signer without placing or cancelling anything.

    Sends a cancel for an order id that cannot exist, so no live order can be affected:
      - unapproved signer -> "User or API Wallet ... does not exist"
      - approved signer   -> "Order was never placed, already canceled, or filled"

    check_signer_identity is only safe against an unregistered signer, because it
    sends an order action. Use this one once a real key is configured.
    """
    action = {"type": "cancel", "cancels": [{"a": trader._instrument(symbol)["assetId"],
                                                "o": UNREACHABLE_OID}]}
    result = trader.submit(action)
    detail = f"{result.get('error') or ''} {json.dumps(result.get('response') or {})}".lower()
    if "does not exist" in detail:
        verdict = "unapproved"
    elif any(marker in detail for marker in ("never placed", "already cancel", "canceled", "cancelled")):
        verdict = "approved"
    else:
        verdict = "unknown"
    return {"verdict": verdict, "outcome": result["outcome"],
            "detail": detail.strip()[:400], "nonce": result.get("nonce")}


def check_signer_identity(trader, symbol, *, transport=None):
    """Prove the signature pipeline by sending an action the exchange must reject.

    Uses an unregistered signer, so nothing can be placed. The exchange replies with
    the address it recovered from the signature; matching it proves keccak, RFC 6979,
    the msgpack ordering, and the EIP-712 phantom-agent digest are all correct.
    """
    instrument = trader._instrument(symbol)
    action = build_order(instrument["assetId"], "buy",
                         format_size(1, instrument["contractValueTradePrecision"]),
                         format_price(1, instrument["contractValueTradePrecision"]), "alo", False)
    nonce = trader.nonce.next()
    signature = sign_l1_action(action, trader.key, nonce, trader.network)
    payload = {"action": action, "nonce": nonce, "signature": signature}
    response = (transport or trader.transport).post(payload)
    detail = response.get("response") if isinstance(response, dict) else None
    text = detail if isinstance(detail, str) else json.dumps(response)
    recovered = re.search(r"0x[0-9a-fA-F]{40}", text or "")
    return {"status": response.get("status") if isinstance(response, dict) else None,
            "detail": text, "expected": trader.address,
            "recovered": recovered.group(0).lower() if recovered else None,
            "matches": bool(recovered) and recovered.group(0).lower() == trader.address}
