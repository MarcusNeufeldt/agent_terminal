"""Bounded read-only exchange backfill and exact observed fill accounting.

The venue retains only the latest 10,000 fills. A completed scan is not proof
of complete account history. No signer or exchange mutation is used here.
"""

from decimal import Decimal, InvalidOperation, localcontext

from hyperliquid_client import HyperliquidError, rows, symbol_for

PAGE_LIMIT = 2000
MAX_PAGES = 16


def decimal_text(value, *, positive=False):
    if isinstance(value, bool) or value is None or len(str(value)) > 128:
        raise HyperliquidError("Invalid fill amount")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise HyperliquidError("Invalid fill amount") from exc
    if not number.is_finite() or abs(number.as_tuple().exponent) > 30 or (positive and number <= 0):
        raise HyperliquidError("Invalid fill amount")
    if number == 0:
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def integer(value, name, *, maximum=2**64 - 1, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise HyperliquidError(f"Invalid fill {name}")
    return value


def normalize(fill):
    coin = fill.get("coin")
    if isinstance(coin, str) and (coin.startswith("@") or any(c in coin for c in "/:")):
        return None  # Stored accounting currently covers native perps only.
    symbol = symbol_for(coin)
    if fill.get("side") not in {"A", "B"}:
        raise HyperliquidError("Invalid fill side")
    fee_token = fill.get("feeToken")
    if fee_token is not None and (not isinstance(fee_token, str) or not fee_token or len(fee_token) > 100):
        raise HyperliquidError("Invalid fill fee token")
    return {"id": str(integer(fill.get("tid"), "trade id")),
            "orderId": str(integer(fill.get("oid"), "order id", minimum=1)),
            "time": integer(fill.get("time"), "timestamp", maximum=2**53 - 1),
            "symbol": symbol, "side": "buy" if fill["side"] == "B" else "sell",
            "size": decimal_text(fill.get("sz"), positive=True),
            "price": decimal_text(fill.get("px"), positive=True),
            "fee": decimal_text(fill["fee"]) if fill.get("fee") is not None else None,
            "feeToken": fee_token,
            "realizedPnl": decimal_text(fill["closedPnl"]) if fill.get("closedPnl") is not None else None}


def sync(db, backend, start, end):
    if not backend.account_configured:
        raise HyperliquidError("Hyperliquid account is not configured")
    start = integer(start, "startTime", maximum=2**53 - 1)
    end = integer(end, "endTime", maximum=2**53 - 1)
    if start > end:
        raise HyperliquidError("startTime must not exceed endTime")
    pending, gaps = [(start, end)], []
    pages = inserted = 0
    while pending and pages < MAX_PAGES:
        low, high = pending.pop()
        try:
            page = rows(backend.client.info("userFillsByTime", user=backend.account_address,
                                          startTime=low, endTime=high, aggregateByTime=False))
            pages += 1
            if len(page) > PAGE_LIMIT:
                raise HyperliquidError("Fill page exceeds documented limit")
            normalized = []
            for raw in page:
                timestamp = integer(raw.get("time"), "timestamp", maximum=2**53 - 1)
                if not low <= timestamp <= high:
                    raise HyperliquidError("Fill timestamp outside requested interval")
                fill = normalize(raw)
                if fill is not None:
                    normalized.append(fill)
            saturated = len(page) == PAGE_LIMIT
            state = "saturated" if saturated else "available-window-scanned"
            inserted += db.save_hyperliquid_fill_page(backend.network, backend.account_address,
                                                      normalized, low, high, state)
            if saturated:
                if low == high:
                    gaps.append({"startTime": low, "endTime": high, "reason": "timestamp-saturated"})
                else:
                    # Split at an observed timestamp so a burst does not consume the
                    # whole page budget bisecting an otherwise empty month.
                    middle = min(high - 1, sorted(raw["time"] for raw in page)[len(page) // 2])
                    pending.extend([(middle + 1, high), (low, middle)])
        except (HyperliquidError, ValueError, TypeError, KeyError) as exc:
            gaps.append({"startTime": low, "endTime": high, "reason": str(exc)})
            # Do not hammer a failed/rate-limited source. Prior pages remain persisted.
            break
    gaps.extend({"startTime": low, "endTime": high, "reason": "not-scanned"} for low, high in pending)
    return {"state": "current" if not gaps else "incomplete", "exchange": "hyperliquid",
            "pages": pages, "inserted": inserted, "scanComplete": not gaps, "gaps": gaps,
            "historyComplete": False, "retentionLimit": 10000, "scope": "native-perps",
            "coverage": "available-api-window", "startTime": start, "endTime": end}


def order_totals(db, backend, order_id):
    if not backend.account_configured:
        raise HyperliquidError("Hyperliquid account is not configured")
    if (not isinstance(order_id, str) or not order_id.isascii() or not order_id.isdigit()
            or len(order_id) > 20 or not 0 < int(order_id) < 2**64):
        raise HyperliquidError("Exact decimal orderId required")
    fills = db.hyperliquid_order_fills(backend.network, backend.account_address, str(int(order_id)))
    identities = {(fill["symbol"], fill["side"]) for fill in fills}
    if len(identities) > 1:
        raise HyperliquidError("Conflicting fill identities for one order")
    with localcontext() as context:
        context.prec = 384
        size = sum((Decimal(fill["size"]) for fill in fills), Decimal(0))
        notional = sum((Decimal(fill["size"]) * Decimal(fill["price"]) for fill in fills), Decimal(0))
        fees = {}
        fees_known = bool(fills) and all(fill["fee"] is not None and fill["feeToken"] for fill in fills)
        for fill in fills:
            if fill["fee"] is not None and fill["feeToken"]:
                token = fill["feeToken"]
                fees[token] = fees.get(token, Decimal(0)) + Decimal(fill["fee"])
        realized = sum((Decimal(fill["realizedPnl"]) for fill in fills), Decimal(0)) if fills and all(
            fill["realizedPnl"] is not None for fill in fills) else None
        return {"state": "current", "exchange": "hyperliquid", "orderId": str(int(order_id)),
                "fillCount": len(fills), "observedFilledSize": str(size), "observedNotional": str(notional),
                "averagePrice": format((notional / size).quantize(Decimal("1e-18")).normalize(), "f") if size else None,
                "averagePriceDecimals": 18,
                "observedFees": {token: str(amount) for token, amount in fees.items()}, "feesKnown": fees_known,
                "observedRealizedPnl": str(realized) if realized is not None else None,
                "symbol": fills[0]["symbol"] if fills else None, "side": fills[0]["side"] if fills else None,
                "historyComplete": False, "coverage": "observed-only"}
