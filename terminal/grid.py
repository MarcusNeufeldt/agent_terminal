"""Fixed-range grid planning. No exchange writes, database, or model arithmetic."""
import hashlib
import json
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING, ROUND_FLOOR


class GridError(ValueError):
    pass


def number(value, label):
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise GridError(f"Enter a valid {label}.") from exc
    if not result.is_finite() or result <= 0:
        raise GridError(f"{label} must be finite and positive.")
    return result


def build_grid_plan(spec, instrument, *, symbol_prefix="PF_", price_rounder=None):
    symbol = str(spec.get("symbol") or "").upper()
    side = spec.get("side")
    if not symbol.startswith(symbol_prefix) or side not in {"buy", "sell"}:
        raise GridError(f"Choose a {symbol_prefix} contract and Buy or Sell.")
    start = number(spec.get("startPrice"), "start price")
    end = number(spec.get("endPrice"), "end price")
    count = number(spec.get("orders"), "order count")
    if count != count.to_integral_value() or not 2 <= count <= 20:
        raise GridError("Choose between 2 and 20 orders.")
    count = int(count)
    if (side == "buy" and start <= end) or (side == "sell" and start >= end):
        raise GridError("Buy grids step down; sell grids step up. Check the start and end prices.")
    order_type = spec.get("orderType", "post")
    if order_type not in {"post", "lmt"}:
        raise GridError("Grid orders must be post-only or limit.")
    if not isinstance(spec.get("reduceOnly", False), bool):
        raise GridError("reduceOnly must be true or false.")
    tick = number(instrument.get("tickSize"), "instrument tick size") if price_rounder is None else None
    try:
        precision = int(instrument["contractValueTradePrecision"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GridError("Instrument size precision is unavailable.") from exc
    lot = Decimal(1).scaleb(-precision)
    mult = number(instrument.get("contractSize", 1), "contract multiplier")
    rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
    raw_prices = [start + (end - start) * i / (count - 1) for i in range(count)]
    prices = [number(price_rounder(price, side), "grid price") if price_rounder else
              (price / tick).to_integral_value(rounding=rounding) * tick for price in raw_prices]
    if min(prices) <= 0 or len(set(prices)) != count:
        raise GridError("Range is too narrow for distinct tick-sized orders. Widen it or use fewer orders.")
    has_size = spec.get("size") is not None
    has_notional = spec.get("notional") is not None
    if has_size == has_notional:
        raise GridError("Supply either total contracts or total USD, not both.")
    warnings = []
    if has_size:
        requested = number(spec["size"], "total contracts")
        units = int((requested / lot).to_integral_value(rounding=ROUND_DOWN))
        base, remainder = divmod(units, count)
        sizes = [(base + (i < remainder)) * lot for i in range(count)]
        if units * lot != requested:
            warnings.append(f"Total contracts rounded down to {units * lot} to meet the lot size.")
    else:
        requested = number(spec["notional"], "total USD")
        sizes = [((requested / count / (price * mult)) / lot).to_integral_value(rounding=ROUND_DOWN) * lot
                 for price in prices]
        warnings.append("USD is split equally per rung; lot rounding can leave some budget unused.")
    if min(sizes) <= 0:
        raise GridError("Size is too small for this many orders. Increase it or use fewer orders.")
    orders = [{"symbol": symbol, "side": side, "orderType": order_type,
               "size": float(size), "limitPrice": float(price), "reduceOnly": spec.get("reduceOnly", False)}
              for price, size in zip(prices, sizes)]
    total_size = sum(sizes)
    total_notional = sum(price * size * mult for price, size in zip(prices, sizes))
    return {"symbol": symbol, "side": side, "orders": orders, "totalSize": float(total_size),
            "notional": float(total_notional), "averagePrice": float(total_notional / total_size / mult),
            "warnings": warnings,
            "previewHash": hashlib.sha256(json.dumps(orders, sort_keys=True).encode()).hexdigest()}


def checked_rows(rows, label):
    if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get("error") for row in rows):
        raise GridError(f"{label} unavailable. Refresh before placing a grid.")
    return rows


def validate_grid(plan, ctx):
    """Recheck current state before submission; never cancel or replace existing orders."""
    symbol, side = plan["symbol"], plan["side"]
    ticker = ctx.fresh_ticker(symbol)
    positions = checked_rows(ctx.get_positions(), "Positions")
    orders = checked_rows(ctx.get_orders(), "Orders")
    rows = plan["orders"]
    reduce_only = rows[0]["reduceOnly"]
    if not reduce_only:
        ctx.require_new_exposure(symbol)
    prices = {number(row["limitPrice"], "grid price") for row in rows}
    for order in orders:
        if order.get("symbol") == symbol and order.get("side") == side and order.get("limitPrice"):
            if number(order["limitPrice"], "existing limit price") in prices:
                raise GridError("An order already exists at a grid price on this side. Existing orders were left untouched.")
    opposite = number(ticker.get("ask" if side == "buy" else "bid"), "executable quote")
    crossing = any(price >= opposite if side == "buy" else price <= opposite for price in prices)
    warnings = []
    if crossing:
        if rows[0]["orderType"] == "post":
            raise GridError("A post-only rung would cross the spread. Use Best bid/ask or move the start price.")
        warnings.append("One or more limit orders can fill immediately and incur taker fees.")
    if reduce_only:
        position = next((p for p in positions if p.get("symbol") == symbol), None)
        expected = "long" if side == "sell" else "short"
        if not position or position.get("side") != expected:
            raise GridError("Reduce-only grid must close an existing position on the opposite side.")
        reserved = sum(number(o.get("unfilledSize", o.get("size")), "working exit size")
                       for o in orders if o.get("symbol") == symbol and o.get("side") == side
                       and str(o.get("reduceOnly")).lower() == "true"
                       and str(o.get("orderType")).lower() in {"lmt", "post", "limit"})
        if sum(number(r["size"], "size") for r in rows) + reserved > number(position["size"], "position size"):
            raise GridError("Grid plus existing limit exits exceeds the current position size. No exits were replaced.")
        warnings.append("Existing TP/SL protection stays unchanged. This adds reduce-only limit exits.")
    return warnings
