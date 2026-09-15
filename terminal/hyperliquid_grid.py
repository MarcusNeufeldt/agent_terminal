"""Native-perp Grid planning and optional read-only preflight. Never places orders."""
from datetime import datetime, timezone
import time
from types import SimpleNamespace
from decimal import Decimal, DecimalException, ROUND_CEILING, ROUND_FLOOR, localcontext

from grid import GridError, build_grid_plan, checked_rows, validate_grid
from hyperliquid_client import HyperliquidError
from hyperliquid_trading import CLOID_RE, format_price, format_size, order_action_for, validate_order_notional


def prepare(spec, backend, cloids):
    """Freeze a Grid wire action. No journal, account validation, signer or transport.

    A future submission handler must persist this exact action, recheck current
    account/order state and permission, then submit once under the ARM lock.
    """
    if (not isinstance(cloids, list) or not 2 <= len(cloids) <= 20 or
            any(not isinstance(value, str) or not CLOID_RE.fullmatch(value) for value in cloids) or
            len(set(cloids)) != len(cloids)):
        raise GridError("Grid requires 2 to 20 distinct client order IDs.")
    if any(key in spec for key in ("cloid", "closePosition", "triggerKind", "triggerPrice", "triggerMarket", "grouping", "tif")):
        raise GridError("Grid preparation supports only ordinary limit orders and per-rung client IDs.")
    if "cloids" in spec and spec["cloids"] != cloids:
        raise GridError("Grid client order identities disagree.")
    symbol = str(spec.get("symbol") or "").strip().upper()
    instrument = dict(backend.markets().get(symbol, {}).get("instrument", {}))
    snapshot = SimpleNamespace(network=backend.network, markets=lambda: {symbol: {"instrument": instrument}})
    plan = preview({**spec, "checkCurrentOrders": False}, snapshot)["plan"]
    if spec.get("previewHash") != plan["previewHash"]:
        raise GridError("Grid preview changed. Review the current plan before preparing orders.")
    if len(cloids) != len(plan["orders"]):
        raise GridError("Grid requires one client order ID per rung.")
    if type(instrument.get("assetId")) is not int or instrument["assetId"] < 0:
        raise GridError("Native asset identity is unavailable.")
    orders = []
    for row, cloid in zip(plan["orders"], cloids):
        order = order_action_for(instrument, row["side"], row["size"], row["limitPrice"],
                                 tif="alo" if row["orderType"] == "post" else "gtc",
                                 reduce_only=row["reduceOnly"], cloid=cloid)["orders"][0]
        if Decimal(order["p"]) != Decimal(str(row["limitPrice"])) or Decimal(order["s"]) != Decimal(str(row["size"])):
            raise GridError("Prepared Grid differs from the reviewed prices or quantities.")
        orders.append(order)
    action = {"type": "order", "orders": orders, "grouping": "na"}
    with localcontext() as context:
        context.prec = 80
        if any(Decimal(order["p"]) * Decimal(order["s"]) < 10 for order in orders):
            raise GridError("Each prepared Grid rung must meet the $10 minimum.")
        if spec.get("size") is not None and sum(Decimal(order["s"]) for order in orders) > Decimal(str(spec["size"])):
            raise GridError("Prepared Grid exceeds the requested contract budget.")
    if spec.get("notional") is not None:
        validate_order_notional(action, spec["notional"])
    return action


def check_current_orders(plan, backend):
    def quote(symbol):
        snapshot = backend.orderbook(symbol, fresh=True)
        stamp = snapshot.get("time")
        if type(stamp) not in (int, float) or not -5000 <= time.time() * 1000 - stamp <= 15000:
            raise GridError("Order book timestamp is missing, stale or ahead of the local clock.")
        book = snapshot["orderBook"]
        return {"bid": book["bids"][0][0], "ask": book["asks"][0][0]}
    def market_eligible(symbol):
        # This is market eligibility, not margin or signing permission.
        instrument = backend.markets().get(symbol, {}).get("instrument", {})
        if not instrument.get("tradeable"):
            raise GridError("Hyperliquid market is not tradeable")
    def positions():
        rows = checked_rows(backend.positions(fresh=True)["positions"], "Positions")
        return [{**row, "size": row.get("sizeExact", row.get("size"))} for row in rows]
    def orders():
        rows = checked_rows(backend.orders(fresh=True)["orders"], "Orders")
        return [{**row, "unfilledSize": row.get("unfilledSizeExact", row.get("unfilledSize", row.get("size")))} for row in rows]
    context = SimpleNamespace(fresh_ticker=quote, get_positions=positions,
                              get_orders=orders, require_new_exposure=market_eligible)
    return validate_grid(plan, context)


def preview(spec, backend):
    requested_check = spec.get("checkCurrentOrders", False)
    if type(requested_check) is not bool:
        raise GridError("checkCurrentOrders must be a boolean")
    symbol = str(spec.get("symbol") or "").strip().upper()
    instrument = backend.markets().get(symbol, {}).get("instrument", {})
    precision = instrument.get("contractValueTradePrecision")
    if (not symbol.startswith("HL_") or instrument.get("symbol") != symbol or
            not instrument.get("tradeable") or type(precision) is not int or not 0 <= precision <= 6):
        raise GridError("Choose a tradeable Hyperliquid native-perp instrument.")
    def round_price(price, side):
        return Decimal(format_price(price, precision, rounding=ROUND_FLOOR if side == "buy" else ROUND_CEILING))
    try:
        plan = build_grid_plan({**spec, "symbol": symbol}, {**instrument, "contractSize": 1},
                               symbol_prefix="HL_", price_rounder=round_price)
    except DecimalException as exc:
        raise GridError("Inputs exceed supported grid precision.") from exc
    for row in plan["orders"]:
        if Decimal(format_size(row["size"], precision)) != Decimal(str(row["size"])):
            raise GridError("Grid size loses precision; use a smaller total amount.")
    if any(Decimal(str(row["size"])) * Decimal(str(row["limitPrice"])) < 10 for row in plan["orders"]):
        plan["warnings"].append("Some rungs are below $10; venue minimums may prevent placement.")
    passed, checked_at, error = None, None, None
    if requested_check:
        try:
            plan["warnings"] += check_current_orders(plan, backend)
            passed = True
        except (GridError, HyperliquidError) as exc:
            passed, error = False, str(exc)
        checked_at = datetime.now(timezone.utc).isoformat()
    plan["warnings"].append("Planning only. Margin has not been validated. Order/quote checks are snapshots, not execution guarantees."
                            if requested_check else "Planning only. Margin, existing orders and reduce-only coverage have not been checked.")
    return {"plan": plan, "exchange": "hyperliquid", "network": backend.network,
            "previewOnly": True, "ready": False, "orderChecksPassed": passed, "orderCheckedAt": checked_at,
            "validationError": error or "Hyperliquid Grid placement is not implemented."}
