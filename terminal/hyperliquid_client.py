"""Read-only Hyperliquid info API. No signer or /exchange transport exists here."""

import math
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hyperliquid.utils.error import ClientError, ServerError
from requests import RequestException
from hyperliquid_sdk_http import SDKTransport

HOSTS = {"mainnet": "api.hyperliquid.xyz", "testnet": "api.hyperliquid-testnet.xyz"}
INFO_TYPES = {"metaAndAssetCtxs", "l2Book", "recentTrades", "candleSnapshot",
              "clearinghouseState", "spotClearinghouseState", "frontendOpenOrders", "userFills",
              "userAbstraction", "orderStatus", "userFillsByTime", "extraAgents", "activeAssetData"}
RESOLUTIONS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600,
               "4h": 14400, "12h": 43200, "1d": 86400, "1w": 604800}
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class HyperliquidError(ValueError):
    pass


def number(value, *, minimum=None, positive=False):
    if isinstance(value, bool) or value is None or value == "":
        raise HyperliquidError("Missing or invalid Hyperliquid numeric field")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HyperliquidError("Invalid Hyperliquid numeric field") from exc
    if not math.isfinite(result) or (minimum is not None and result < minimum) or (positive and result <= 0):
        raise HyperliquidError("Out-of-range Hyperliquid numeric field")
    return result


def optional_number(value):
    return None if value is None else number(value)


def rows(value):
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise HyperliquidError("Invalid Hyperliquid list response")
    return value


def symbol_for(coin):
    if not isinstance(coin, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", coin):
        raise HyperliquidError("Unsupported Hyperliquid coin")
    # Separate namespace prevents an HL asset ever passing Kraken's PF_ guard.
    return "HL_" + coin.upper()


def iso_time(value):
    return datetime.fromtimestamp(number(value, positive=True) / 1000, timezone.utc).isoformat()


def trade_row(row, coin):
    if not isinstance(row, dict) or row.get("coin") != coin or row.get("side") not in {"A", "B"}:
        raise HyperliquidError("Invalid Hyperliquid trade identity")
    trade_id = row.get("tid")
    if type(trade_id) is not int or trade_id < 0:
        raise HyperliquidError("Invalid Hyperliquid trade ID")
    return {"symbol": symbol_for(coin), "price": number(row.get("px"), positive=True),
            "qty": number(row.get("sz"), positive=True), "time": number(row.get("time"), positive=True) / 1000,
            "side": "buy" if row["side"] == "B" else "sell", "id": str(trade_id)}


def candle_row(row, coin, resolution):
    if not isinstance(row, dict) or row.get("s") != coin or row.get("i") != resolution:
        raise HyperliquidError("Invalid Hyperliquid candle identity")
    timestamp = number(row.get("t"), positive=True)
    if timestamp != int(timestamp) or int(timestamp) % 1000:
        raise HyperliquidError("Invalid Hyperliquid candle timestamp")
    values = [number(row.get(key), positive=True) for key in ("o", "h", "l", "c")]
    op, high, low, close = values
    if not low <= min(op, close) <= max(op, close) <= high:
        raise HyperliquidError("Invalid Hyperliquid candle range")
    return [int(timestamp / 1000), *values, number(row.get("v"), minimum=0)]


class HyperliquidClient:
    def __init__(self, network="mainnet", *, transport=None):
        if network not in HOSTS:
            raise HyperliquidError("HYPERLIQUID_NETWORK must be mainnet or testnet")
        self.network = network
        self.host = HOSTS[network]
        self._transport = transport if transport is not None else SDKTransport(f"https://{self.host}", timeout=12, max_bytes=8 * 1024 * 1024)
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def info(self, kind, **params):
        if kind not in INFO_TYPES or "type" in params:
            raise HyperliquidError("Only supported read-only Hyperliquid info requests are allowed")
        with self._lock:
            if time.monotonic() < self._blocked_until:
                raise HyperliquidError("Hyperliquid rate limit cooldown; retry later")
        try:
            payload = self._transport.post("/info", {"type": kind, **params})
            # userAbstraction answers with a bare string, so str is a valid shape here.
            if isinstance(payload, dict) and payload.get("error"):
                raise HyperliquidError("Hyperliquid returned an error response")
            if not isinstance(payload, (dict, list, str)):
                raise HyperliquidError("Hyperliquid returned an invalid info response")
            return payload
        except ClientError as exc:
            if exc.status_code in {418, 429}:
                delay = 60.0
                retry_after = exc.header.get("Retry-After", "") if exc.header else ""
                try:
                    delay = max(delay, number(retry_after, minimum=0))
                except HyperliquidError:
                    try:
                        delay = max(delay, parsedate_to_datetime(retry_after).timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
                with self._lock:
                    self._blocked_until = time.monotonic() + delay
            raise HyperliquidError(f"Hyperliquid info HTTP {exc.status_code}") from exc
        except ServerError as exc:
            raise HyperliquidError(f"Hyperliquid info HTTP {exc.status_code}") from exc
        except (RequestException, OSError, ValueError) as exc:
            if isinstance(exc, HyperliquidError):
                raise
            raise HyperliquidError(f"Hyperliquid info read failed: {type(exc).__name__}") from exc
