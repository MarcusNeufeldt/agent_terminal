"""Trading actions the AI assistant (or UI) can propose and execute.

Mirrors the kraken-futures-cli workflows that map onto raw REST calls:
grid/ladder entry, market close/reduce, take-profit replacement, cancels.
Every handler runs in two modes:
  - armed: sends real orders to Kraken
  - disarmed: computes and returns the exact plan, touches nothing

Action schema (JSON):
  {"type": "order",      "symbol", "side", "orderType", "size", "limitPrice?", "stopPrice?", "reduceOnly?"}
  {"type": "ladder",     "symbol", "side", "notional", "orders", "depthPercent", "orderType?"="post", "includeCurrent?"=false, "reduceOnly?"}
  {"type": "close",      "symbol", "percent?"=100, "size?"}
  {"type": "replace_tp", "symbol", "stopPrice"}
  {"type": "replace_sl", "symbol", "stopPrice"}
  {"type": "cancel_all",        "symbol"}
  {"type": "cancel_all_orders"}
  {"type": "cancel",            "cliOrdId"}
  {"type": "chase",      "symbol", "side", "size", "reduceOnly?"}
"""

from __future__ import annotations

import time
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Callable

from exchange_ops import ensure_client_id, parse_operation, unique_client_id
from kraken_client import KrakenFuturesError
from grid import GridError, build_grid_plan, validate_grid

ACTION_TYPES = {"order", "ladder", "close", "replace_tp", "replace_sl", "cancel_all", "cancel_all_orders", "cancel", "chase"}
ORDER_TYPES = {"mkt", "lmt", "post", "ioc", "stp", "take_profit"}
LIMIT_TYPES = {"lmt", "post", "ioc"}
TRIGGER_TYPES = {"stp", "take_profit"}
MANAGED_TP_PREFIX = "kt-full-tp-"
MANAGED_SL_PREFIX = "kt-full-sl-"
# A TP is a resting reduce-only LIMIT on the exit side (a maker TP), or the older mark
# trigger. Kraken reports a post-only limit as lmt or post. Chase orders (ch-) are
# reduce-only limits too, but they are exits in flight, not protection.
TP_LIMIT_TYPES = {"lmt", "post"}
PROTECTION_ORDER_TYPES = {"TP": {"take_profit"} | TP_LIMIT_TYPES, "SL": {"stp", "stop"}}
CHASE_CLIENT_PREFIX = "ch-"


def is_protection_order(order: dict[str, Any], kind: str) -> bool:
    """One definition of a TP/SL order for every check: reduce-only, of the kind's types,
    and for a limit TP not a Chase order."""
    if not isinstance(order, dict) or str(order.get("reduceOnly")).lower() != "true":
        return False
    order_type = str(order.get("orderType") or "").lower()
    if order_type not in PROTECTION_ORDER_TYPES.get(kind, set()):
        return False
    return not (order_type in TP_LIMIT_TYPES and str(order.get("cliOrdId") or "").startswith(CHASE_CLIENT_PREFIX))


def protection_price(order: dict[str, Any]) -> Any:
    """The price a TP/SL exits at: the limit for a maker TP, the trigger otherwise."""
    if str(order.get("orderType") or "").lower() in TP_LIMIT_TYPES:
        return order.get("limitPrice")
    return order.get("stopPrice")
PRICE_MAX_AGE_SECONDS = 5.0


class ActionError(Exception):
    pass


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except Exception as exc:
        raise ActionError(f"invalid number: {value!r}") from exc


def _round_tick(value: Decimal, tick: Decimal, direction: str) -> Decimal:
    rounding = ROUND_FLOOR if direction == "down" else ROUND_CEILING
    quotient = value / tick
    return quotient.to_integral_value(rounding=rounding) * tick


def _round_size(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)


def _fmt(value: Decimal) -> float:
    return float(value)


def _as_number(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def protection_covers(orders: list[dict[str, Any]], symbol: str, kind: str, position_size: Any) -> bool:
    """True when reduce-only protection of this kind covers the whole position.

    Hyperliquid reports native position TP/SL with the exchange's zero-size
    sentinel, because the venue resolves the full position at trigger time.
    Summing the reported size would score a fully protected position as naked
    and leave its alert permanently unclearable, so the flag wins over the size.
    """
    size = _as_number(position_size)
    if size is None or size <= 0:
        return False
    if kind not in PROTECTION_ORDER_TYPES:
        return False
    coverage = 0.0
    for order in orders:
        if str(order.get("symbol") or "") != symbol or not is_protection_order(order, kind):
            continue
        if order.get("positionTpsl") is True:
            return True
        reported = order.get("unfilledSize")
        amount = _as_number(order.get("size") if reported is None else reported)
        if amount is None:
            continue
        coverage += amount
    return coverage >= size


class ActionContext:
    """Everything the handlers need; supplied by server.py."""

    def __init__(
        self,
        *,
        client,
        hub,
        get_account: Callable[[], dict],
        get_positions: Callable[[], list[dict]],
        get_orders: Callable[[], list[dict]],
        get_instruments: Callable[[], dict],
        get_ticker_rest: Callable[[str], dict | None],
        chase: Any = None,
    ) -> None:
        self.client = client
        self.hub = hub
        self.get_account = get_account
        self.get_positions = get_positions
        self.get_orders = get_orders
        self.get_instruments = get_instruments
        self.get_ticker_rest = get_ticker_rest
        self.chase = chase
        self.start_chase: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.refresh_orders: Callable[[], None] | None = None
        self.set_protection_alert: Callable[[str, str, dict[str, Any]], None] | None = None
        self.clear_protection_alert: Callable[[str, str], None] | None = None
        self.after_action: Callable[[dict[str, Any], dict[str, Any], bool], None] | None = None
        self._instrument_cache: dict[str, dict] | None = None

    def instrument(self, symbol: str) -> dict:
        if self._instrument_cache is None:
            try:
                instruments = self.get_instruments().get("instruments", [])
            except KrakenFuturesError:
                instruments = []
            self._instrument_cache = {str(i.get("symbol")): i for i in instruments}
        return self._instrument_cache.get(symbol, {})

    @staticmethod
    def _ticker_age(ticker: dict[str, Any]) -> float | None:
        stamp = ticker.get("_receivedAt") or ticker.get("time")
        try:
            return max(0.0, time.time() - float(stamp))
        except (TypeError, ValueError):
            return None

    def fresh_ticker(self, symbol: str) -> dict[str, Any]:
        ticker = self.hub.ticker(symbol) or {}
        age = self._ticker_age(ticker)
        if age is not None and age <= PRICE_MAX_AGE_SECONDS:
            return ticker
        ticker = self.get_ticker_rest(symbol) or {}
        age = self._ticker_age(ticker)
        if age is None or age > PRICE_MAX_AGE_SECONDS:
            detail = f" ({age:.1f}s old)" if age is not None else ""
            raise ActionError(f"current market data unavailable or stale for {symbol}{detail}")
        return ticker

    def current_price(self, symbol: str) -> Decimal:
        ticker = self.fresh_ticker(symbol)
        price = ticker.get("last") or ticker.get("markPrice")
        if not price:
            raise ActionError(f"no current price available for {symbol}")
        return _dec(price)

    def mark_price(self, symbol: str) -> Decimal:
        ticker = self.fresh_ticker(symbol)
        price = ticker.get("markPrice")
        if not price:
            raise ActionError(f"no mark price available for {symbol}")
        return _dec(price)

    def require_new_exposure(self, symbol: str) -> None:
        account = self.get_account()
        if not isinstance(account, dict) or account.get("error") or not account:
            error = account.get("error") if isinstance(account, dict) else "invalid response"
            raise ActionError(f"account state unavailable; new exposure rejected: {error}")
        self.fresh_ticker(symbol)


def apply_size_precision(params: dict[str, Any], instrument: dict[str, Any]) -> dict[str, Any]:
    """Round size down to the instrument's contract precision (PF_PUMPUSD uses -2 = lots of 100)."""
    precision = int(instrument.get("contractValueTradePrecision") or 0)
    size = _round_size(_dec(params["size"]), precision)
    if size <= 0:
        raise ActionError(f"size rounds to zero for {params['symbol']} (size precision {precision})")
    params["size"] = float(size)
    return params


def _validate_order(a: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    symbol = str(a.get("symbol") or "").strip().upper()
    side = str(a.get("side") or "").strip().lower()
    order_type = str(a.get("orderType") or "").strip().lower()
    size = a.get("size")
    if not symbol.startswith("PF_"):
        raise ActionError("PF_ symbol required")
    if side not in {"buy", "sell"}:
        raise ActionError("side must be buy or sell")
    if order_type not in ORDER_TYPES:
        raise ActionError(f"orderType must be one of {sorted(ORDER_TYPES)}")
    size_d = _dec(size)
    if size_d <= 0:
        raise ActionError("size must be positive")
    params: dict[str, Any] = {
        "symbol": symbol, "side": side, "orderType": order_type,
    }
    params = apply_size_precision({**params, "size": _fmt(size_d)}, ctx.instrument(symbol))
    params["side"] = side
    params["orderType"] = order_type
    instrument = ctx.instrument(symbol)
    tick = _dec(instrument.get("tickSize") or "0.00000001")

    def to_tick(value: Any) -> float:
        d = _dec(value)
        if d <= 0:
            raise ActionError("price must be positive")
        return float((d / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick)

    if order_type in LIMIT_TYPES:
        limit = to_tick(a.get("limitPrice"))
        params["limitPrice"] = limit
    if order_type in TRIGGER_TYPES:
        stop = to_tick(a.get("stopPrice"))
        params["stopPrice"] = stop
        params["triggerSignal"] = str(a.get("triggerSignal") or "mark")
    if a.get("reduceOnly"):
        params["reduceOnly"] = True
    cli_order_id = str(a.get("cliOrdId") or "").strip()
    if cli_order_id:
        params["cliOrdId"] = cli_order_id
    return params


def _ladder_plan(a: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    if "startPrice" in a or "endPrice" in a:
        try:
            return build_grid_plan(a, ctx.instrument(str(a.get("symbol") or "").upper()))
        except GridError as exc:
            raise ActionError(str(exc)) from exc
    symbol = str(a.get("symbol") or "").strip().upper()
    side = str(a.get("side") or "").strip().lower()
    if not symbol.startswith("PF_"):
        raise ActionError("PF_ symbol required")
    if side not in {"buy", "sell"}:
        raise ActionError("side must be buy or sell")
    notional = _dec(a.get("notional"))
    orders_n = int(a.get("orders") or 0)
    depth = _dec(a.get("depthPercent"))
    order_type = str(a.get("orderType") or "post").strip().lower()
    include_current = bool(a.get("includeCurrent"))
    if notional <= 0:
        raise ActionError("notional must be positive (USD)")
    if not 1 <= orders_n <= 20:
        raise ActionError("orders must be 1-20")
    if depth < 0:
        raise ActionError("depthPercent must be non-negative")
    if order_type not in {"lmt", "post"}:
        raise ActionError("ladder orderType must be lmt or post")

    instrument = ctx.instrument(symbol)
    tick = _dec(instrument.get("tickSize") or "0.00000001")
    precision = int(instrument.get("contractValueTradePrecision") or 0)
    is_inverse = str(instrument.get("type")) == "futures_inverse"
    contract_size = _dec(instrument.get("contractSize") or 1)

    current = _dec(ctx.current_price(symbol))
    per_notional = notional / orders_n
    denom = orders_n - 1 if include_current and orders_n > 1 else orders_n
    step = depth / denom

    built = []
    for i in range(1, orders_n + 1):
        offset = Decimal(0) if (include_current and orders_n > 1 and i == 1) else step * (i if not include_current else i - 1)
        if side == "buy":
            price = _round_tick(current * (Decimal(1) - offset / 100), tick, "down")
        else:
            price = _round_tick(current * (Decimal(1) + offset / 100), tick, "up")
        if price <= 0:
            raise ActionError("calculated non-positive limit price")
        # inverse contracts (PI_*): 1 contract = contractSize USD notional, so
        # size = notional / contractSize. Linear (PF_*): notional = size * price.
        if is_inverse:
            size = _round_size(per_notional / contract_size, precision)
        else:
            size = _round_size(per_notional / price, precision)
        if size <= 0:
            raise ActionError("order size rounds to zero; increase notional or use fewer orders")
        built.append({
            "orderType": order_type,
            "symbol": symbol,
            "side": side,
            "size": _fmt(size),
            "limitPrice": _fmt(price),
            "reduceOnly": bool(a.get("reduceOnly")) or None,
        })

    plan_orders = []
    for o in built:
        o = {k: v for k, v in o.items() if v is not None}
        plan_orders.append(o)
    return {
        "symbol": symbol,
        "side": side,
        "currentPrice": _fmt(current),
        "notional": _fmt(notional),
        "depthPercent": _fmt(depth),
        "orders": plan_orders,
    }


def _checked_rows(rows: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    error = next((row.get("error") for row in rows if isinstance(row, dict) and row.get("error")), None)
    if error:
        raise ActionError(f"cannot read {label}: {error}")
    return rows


def _close_plan(a: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    symbol = str(a.get("symbol") or "").strip().upper()
    if not symbol.startswith("PF_"):
        raise ActionError("PF_ symbol required")
    positions = _checked_rows(ctx.get_positions(), "positions")
    position = next((p for p in positions if str(p.get("symbol")) == symbol), None)
    if not position:
        raise ActionError(f"no open position on {symbol}")
    size_total = _dec(position.get("size"))
    side_pos = str(position.get("side") or "").lower()
    if "size" in a and a.get("size") is not None:
        size = _dec(a.get("size"))
        if size <= 0 or size > size_total:
            raise ActionError(f"size must be within 0..{size_total}")
    else:
        percent = _dec(a.get("percent") if a.get("percent") is not None else 100)
        if not Decimal(0) < percent <= 100:
            raise ActionError("percent must be 1-100")
        size = size_total * percent / 100
    instrument = ctx.instrument(symbol)
    precision = int(instrument.get("contractValueTradePrecision") or 0)
    size = _round_size(size, precision)
    if size <= 0:
        raise ActionError("close size rounds to zero")
    return {
        "symbol": symbol,
        "orderType": "mkt",
        "side": "sell" if side_pos == "long" else "buy",
        "size": _fmt(size),
        "remainingSize": _fmt(size_total - size),
        "reduceOnly": True,
        "closingPercent": _fmt(size / size_total * 100) if size_total else None,
    }


def _exchange_order_id(order: dict[str, Any]) -> str:
    return str(order.get("order_id") or order.get("orderId") or "")


def _new_protection_client_id(symbol: str, order_type: str, managed: bool, purpose: str = "") -> str:
    if managed:
        prefix = MANAGED_TP_PREFIX if order_type == "take_profit" else MANAGED_SL_PREFIX
    else:
        prefix = f"kt-{purpose or 'protection'}-"
    return unique_client_id(f"{prefix}{symbol}")


def _normalized_trigger_signal(value: Any) -> str:
    signal = str(value or "mark").lower().replace("_price", "")
    return signal if signal in {"mark", "index", "last"} else "mark"


def _restore_protection_order(order: dict[str, Any], ctx: ActionContext, order_type: str) -> dict[str, Any]:
    size = order.get("unfilledSize") if order.get("unfilledSize") is not None else order.get("size")
    if str(order.get("orderType") or "").lower() in TP_LIMIT_TYPES:
        # A maker TP comes back as the post-only limit it was.
        return _validate_order({"orderType": "post", "symbol": order.get("symbol"), "side": order.get("side"),
                                "size": size, "limitPrice": order.get("limitPrice"), "reduceOnly": True}, ctx)
    trigger_signal = _normalized_trigger_signal(order.get("triggerSignal"))
    restored = {
        "orderType": order_type,
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "size": order.get("unfilledSize") if order.get("unfilledSize") is not None else order.get("size"),
        "stopPrice": order.get("stopPrice"),
        "triggerSignal": trigger_signal,
        "reduceOnly": True,
    }
    return _validate_order(restored, ctx)


def _replace_protection_plan(a: dict[str, Any], ctx: ActionContext, order_type: str) -> dict[str, Any]:
    symbol = str(a.get("symbol") or "").strip().upper()
    if not symbol.startswith("PF_"):
        raise ActionError("PF_ symbol required")
    positions = _checked_rows(ctx.get_positions(), "positions")
    position = next((p for p in positions if str(p.get("symbol")) == symbol), None)
    if not position:
        raise ActionError(f"no open position on {symbol}")

    side_pos = str(position.get("side") or "").lower()
    if side_pos not in {"long", "short"}:
        raise ActionError(f"unknown position side {side_pos!r}")
    stop = _dec(a.get("stopPrice"))
    if stop <= 0:
        raise ActionError("stopPrice must be positive")
    maker_tp = order_type == "take_profit"
    if maker_tp:
        # The TP is a post-only limit: it must rest on the exit side of the book, or
        # Kraken rejects it for crossing (a sell at or below the bid, a buy at or above the ask).
        ticker = ctx.fresh_ticker(symbol)
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if not bid or not ask:
            raise ActionError(f"no current bid/ask for {symbol}; a maker take profit needs the book")
        if side_pos == "long" and stop <= _dec(bid):
            raise ActionError(f"take profit must be above the best bid {bid} to rest as a maker order")
        if side_pos == "short" and stop >= _dec(ask):
            raise ActionError(f"take profit must be below the best ask {ask} to rest as a maker order")
    else:
        mark = ctx.mark_price(symbol)
        must_be_above = side_pos == "short"
        if stop == mark or (stop > mark) != must_be_above:
            direction = "above" if must_be_above else "below"
            raise ActionError(f"stop loss price must be {direction} current mark {mark}")

    orders = _checked_rows(ctx.get_orders(), "open orders")
    kind = "TP" if maker_tp else "SL"
    existing = [
        order for order in orders
        if isinstance(order, dict)
        and str(order.get("symbol")) == symbol
        and is_protection_order(order, kind)
    ]
    requested_order_id = str(a.get("orderId") or a.get("order_id") or a.get("sourceOrderId") or "")
    requested_cli_id = str(a.get("cliOrdId") or a.get("sourceCliOrdId") or "")
    target = None
    if requested_order_id or requested_cli_id:
        target = next((
            order for order in existing
            if (requested_order_id and _exchange_order_id(order) == requested_order_id)
            or (requested_cli_id and str(order.get("cliOrdId") or "") == requested_cli_id)
        ), None)
        if target is None:
            raise ActionError("exact protection order is no longer open; refresh before retrying")
    elif len(existing) == 1:
        target = existing[0]
    elif len(existing) > 1:
        raise ActionError("multiple protection orders form a ladder; provide one exact order ID")

    size = _dec(position.get("size"))
    if target is not None and a.get("preserveSize"):
        size = _dec(target.get("unfilledSize") if target.get("unfilledSize") is not None else target.get("size"))
    managed = bool(a.get("managed", True))
    order_input = {
        "orderType": order_type,
        "symbol": symbol,
        "side": "sell" if side_pos == "long" else "buy",
        "size": _fmt(size),
        "stopPrice": _fmt(stop),
        "triggerSignal": "mark",
        "reduceOnly": True,
    }
    if maker_tp:
        # Maker TP: a resting post-only reduce-only limit at the TP price.
        order_input = {"orderType": "post", "symbol": symbol, "side": order_input["side"],
                       "size": order_input["size"], "limitPrice": _fmt(stop), "reduceOnly": True}
    if target and target.get("cliOrdId"):
        order_input["cliOrdId"] = str(target["cliOrdId"])
    elif target is None:
        order_input["cliOrdId"] = _new_protection_client_id(symbol, order_type, managed)
    new_order = _validate_order(order_input, ctx)
    previous_order = _restore_protection_order(target, ctx, order_type) if target else None
    order_id = _exchange_order_id(target) if target else ""
    target_cli_id = str(target.get("cliOrdId") or "") if target else ""
    if target and not order_id and not target_cli_id:
        raise ActionError("existing protection has no exact exchange or client order ID")
    cancel_target = ({"order_id": order_id} if order_id else {"cliOrdId": target_cli_id}) if target else None
    target_is_limit = bool(target) and str(target.get("orderType") or "").lower() in TP_LIMIT_TYPES
    if maker_tp:
        # A limit TP is edited in place; a legacy trigger TP is cancelled and replaced by one.
        can_edit = bool(order_id) and target_is_limit
        edit_params = {"orderId": order_id, "size": new_order["size"], "limitPrice": new_order["limitPrice"]} if can_edit else None
    else:
        can_edit = bool(order_id) and _normalized_trigger_signal(target.get("triggerSignal")) == "mark"
        edit_params = {"orderId": order_id, "size": new_order["size"], "stopPrice": new_order["stopPrice"]} if can_edit else None
    return {
        "target": target,
        "orderId": order_id or None,
        "cancelTarget": cancel_target,
        "editParams": edit_params,
        "previousOrder": previous_order,
        "order": new_order,
        "managed": managed,
    }


def _operation_detail(response: Any, key: str, expected: str) -> dict[str, Any]:
    ambiguous = ("notFound", "orderForEditNotFound") if key in {"cancelStatus", "editStatus"} else ()
    parsed = parse_operation(response, key, expected, ambiguous_statuses=ambiguous)
    if parsed["outcome"] != "confirmed":
        raise ActionError(parsed.get("error") or f"Kraken {key} was not confirmed")
    return parsed["detail"]


def _fresh_orders(ctx: ActionContext) -> list[dict[str, Any]]:
    refresh = getattr(ctx, "refresh_orders", None)
    if refresh:
        refresh()
    return _checked_rows(ctx.get_orders(), "open orders")


def _find_open_protection(ctx: ActionContext, target: dict[str, Any]) -> dict[str, Any] | None:
    order_id = _exchange_order_id(target)
    cli_id = str(target.get("cliOrdId") or "")
    return next((
        order for order in _fresh_orders(ctx)
        if (order_id and _exchange_order_id(order) == order_id)
        or (cli_id and str(order.get("cliOrdId") or "") == cli_id)
    ), None)


def _protection_matches(order: dict[str, Any], desired: dict[str, Any]) -> bool:
    size = order.get("unfilledSize") if order.get("unfilledSize") is not None else order.get("size")
    desired_size = desired.get("unfilledSize") if desired.get("unfilledSize") is not None else desired.get("size")
    try:
        return _dec(protection_price(order)) == _dec(protection_price(desired)) and _dec(size) == _dec(desired_size)
    except ActionError:
        return False


def _submitted_status(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any] | None:
    cli_id = str(params.get("cliOrdId") or "")
    if not cli_id:
        return None
    try:
        response = ctx.client.post("/orders/status", params={"cliOrdIds": cli_id}, private=True)
    except Exception:
        return None
    rows = response.get("orders") if isinstance(response, dict) and response.get("result") == "success" else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        order = row.get("order") if isinstance(row, dict) else None
        if isinstance(order, dict) and str(order.get("cliOrdId") or "") == cli_id:
            status = str(row.get("status") or "").upper()
            if status in {"ENTERED_BOOK", "TRIGGER_PLACED", "FULLY_EXECUTED"}:
                return {"status": status, "order": order}
    return None


def submit_one(ctx: ActionContext, params: dict[str, Any], prefix: str) -> dict[str, Any]:
    prepared = ensure_client_id(params, prefix)
    try:
        response = ctx.client.post("/sendorder", params=prepared, private=True)
    except Exception as exc:
        reconciled = _submitted_status(ctx, prepared)
        if reconciled:
            return {
                "outcome": "confirmed", "params": prepared, "reconciled": reconciled,
                "exchangeId": reconciled["order"].get("orderId"),
                "nestedStatus": reconciled["status"], "verification": {"source": "orders/status"},
                "transportError": f"{type(exc).__name__}: {exc}",
            }
        return {
            "outcome": "unknown", "params": prepared, "nestedStatus": None,
            "verification": {"source": "orders/status", "confirmed": False},
            "error": f"{type(exc).__name__}: {exc}",
        }
    parsed = parse_operation(response, "sendStatus", "placed")
    if parsed["outcome"] != "confirmed":
        reconciled = _submitted_status(ctx, prepared) if parsed["outcome"] == "unknown" else None
        if reconciled:
            return {
                "outcome": "confirmed", "params": prepared, "response": response,
                "reconciled": reconciled, "exchangeId": reconciled["order"].get("orderId"),
                "nestedStatus": reconciled["status"], "verification": {"source": "orders/status"},
            }
        return {
            "outcome": parsed["outcome"], "params": prepared, "response": response,
            "nestedStatus": parsed.get("nestedStatus"), "exchangeId": parsed.get("exchangeId"),
            "verification": {"source": "sendStatus"}, "error": parsed.get("error"),
        }
    return {
        "outcome": "confirmed", "params": prepared, "response": response,
        "nestedStatus": parsed["nestedStatus"], "exchangeId": parsed.get("exchangeId"),
        "verification": {"source": "sendStatus"},
    }


def _submit_protection(ctx: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    return submit_one(ctx, params, "kt-protection")


def _find_open_order(ctx: ActionContext, target: dict[str, Any]) -> dict[str, Any] | None:
    order_id = str(target.get("order_id") or target.get("orderId") or "")
    cli_id = str(target.get("cliOrdId") or "")
    return next((
        order for order in _fresh_orders(ctx)
        if (order_id and _exchange_order_id(order) == order_id)
        or (cli_id and str(order.get("cliOrdId") or "") == cli_id)
    ), None)


def cancel_one(ctx: ActionContext, target: dict[str, Any]) -> dict[str, Any]:
    try:
        response = ctx.client.post("/cancelorder", params=target, private=True)
    except Exception as exc:
        try:
            current = _find_open_order(ctx, target)
        except ActionError as read_exc:
            return {
                "outcome": "unknown", "target": target, "nestedStatus": None,
                "verification": {"source": "openorders", "state": "unavailable"},
                "error": f"cancel transport failed ({type(exc).__name__}: {exc}); verification failed ({read_exc})",
            }
        return {
            "outcome": "unknown", "target": target, "nestedStatus": None,
            "verification": {"source": "openorders", "open": current is not None},
            "error": f"{type(exc).__name__}: {exc}",
        }
    parsed = parse_operation(response, "cancelStatus", "cancelled", ambiguous_statuses=("notFound",))
    if parsed["outcome"] == "confirmed":
        return {
            "outcome": "confirmed", "target": target, "response": response,
            "nestedStatus": parsed["nestedStatus"], "exchangeId": parsed.get("exchangeId"),
            "verification": {"source": "cancelStatus"},
        }
    try:
        current = _find_open_order(ctx, target)
    except ActionError as read_exc:
        return {
            "outcome": "unknown", "target": target, "response": response,
            "nestedStatus": parsed.get("nestedStatus"), "exchangeId": parsed.get("exchangeId"),
            "verification": {"source": "openorders", "state": "unavailable"},
            "error": f"{parsed.get('error') or 'cancel not confirmed'}; verification failed ({read_exc})",
        }
    if current is None and parsed["outcome"] == "unknown":
        return {
            "outcome": "confirmed", "target": target, "response": response,
            "nestedStatus": parsed.get("nestedStatus"), "exchangeId": parsed.get("exchangeId"),
            "verification": {"source": "openorders", "open": False},
        }
    return {
        "outcome": parsed["outcome"], "target": target, "response": response,
        "nestedStatus": parsed.get("nestedStatus"), "exchangeId": parsed.get("exchangeId"),
        "verification": {"source": "openorders", "open": current is not None},
        "error": parsed.get("error"),
    }


def _set_unprotected(ctx: ActionContext, symbol: str, kind: str, details: dict[str, Any]) -> None:
    callback = getattr(ctx, "set_protection_alert", None)
    if callback:
        callback(symbol, kind, details)


def _clear_unprotected(ctx: ActionContext, symbol: str, kind: str) -> None:
    callback = getattr(ctx, "clear_protection_alert", None)
    if callback:
        callback(symbol, kind)


def _execute_protection_change(kind: str, plan: dict[str, Any], ctx: ActionContext) -> dict[str, Any]:
    symbol = str(plan["order"]["symbol"])
    alert_kind = "TP" if kind == "replace_tp" else "SL"
    target = plan.get("target")
    edit_params = plan.get("editParams")

    if edit_params:
        try:
            response = ctx.client.post("/editorder", params=edit_params, private=True)
        except Exception as exc:
            try:
                current = _find_open_protection(ctx, target)
            except ActionError:
                current = None
            if current and _protection_matches(current, plan["order"]):
                _clear_unprotected(ctx, symbol, alert_kind)
                return {"type": kind, "ok": True, "outcome": "confirmed", "edited": True,
                        "orderId": plan["orderId"], "order": plan["order"], "reconciledAfterError": str(exc)}
            if current and _protection_matches(current, target):
                return {"type": kind, "ok": False, "outcome": "rejected", "originalWorking": True,
                        "orderId": plan["orderId"], "error": f"edit failed; original order remains: {exc}"}
            details = {"message": f"edit outcome unknown: {type(exc).__name__}: {exc}", "orderId": plan["orderId"]}
            _set_unprotected(ctx, symbol, alert_kind, details)
            return {"type": kind, "ok": False, "outcome": "unknown", "error": details["message"]}
        try:
            detail = _operation_detail(response, "editStatus", "edited")
        except ActionError as exc:
            try:
                current = _find_open_protection(ctx, target)
            except ActionError:
                current = None
            if current and _protection_matches(current, plan["order"]):
                _clear_unprotected(ctx, symbol, alert_kind)
                return {"type": kind, "ok": True, "outcome": "confirmed", "edited": True,
                        "orderId": plan["orderId"], "order": plan["order"], "response": response,
                        "reconciledAfterReject": True}
            if current and _protection_matches(current, target):
                return {"type": kind, "ok": False, "outcome": "rejected", "originalWorking": True,
                        "orderId": plan["orderId"], "response": response, "error": str(exc)}
            details = {"message": f"edit rejected and original protection is not confirmed unchanged: {exc}", "orderId": plan["orderId"]}
            _set_unprotected(ctx, symbol, alert_kind, details)
            return {"type": kind, "ok": False, "outcome": "unknown", "response": response, "error": details["message"]}
        _clear_unprotected(ctx, symbol, alert_kind)
        return {"type": kind, "ok": True, "outcome": "confirmed", "edited": True,
                "orderId": plan["orderId"], "editStatus": detail, "response": response, "order": plan["order"]}

    if target is None:
        submitted = _submit_protection(ctx, plan["order"])
        if submitted["outcome"] == "confirmed":
            _clear_unprotected(ctx, symbol, alert_kind)
            return {"type": kind, "ok": True, "order": plan["order"], **submitted}
        details = {"message": f"new protection {submitted['outcome']}: {submitted.get('error') or 'unknown'}"}
        _set_unprotected(ctx, symbol, alert_kind, details)
        return {"type": kind, "ok": False, "order": plan["order"], **submitted, "error": details["message"]}

    cancel_target = plan["cancelTarget"]
    try:
        cancel_response = ctx.client.post("/cancelorder", params=cancel_target, private=True)
        _operation_detail(cancel_response, "cancelStatus", "cancelled")
    except Exception as exc:
        try:
            original = _find_open_protection(ctx, target)
        except ActionError:
            original = None
        if original and _protection_matches(original, target):
            return {"type": kind, "ok": False, "outcome": "rejected", "originalWorking": True,
                    "error": f"cancellation not confirmed; no replacement sent: {exc}"}
        details = {"message": f"cancellation outcome unknown: {type(exc).__name__}: {exc}"}
        _set_unprotected(ctx, symbol, alert_kind, details)
        return {"type": kind, "ok": False, "outcome": "unknown", "error": details["message"]}

    replacement = dict(plan["order"])
    replacement["cliOrdId"] = _new_protection_client_id(symbol, replacement["orderType"], plan["managed"], "replace")
    submitted = _submit_protection(ctx, replacement)
    if submitted["outcome"] == "confirmed":
        _clear_unprotected(ctx, symbol, alert_kind)
        return {"type": kind, "ok": True, "order": replacement, "cancelResponse": cancel_response, **submitted}
    if submitted["outcome"] == "unknown":
        details = {"message": f"replacement outcome unknown: {submitted.get('error') or 'unknown'}"}
        _set_unprotected(ctx, symbol, alert_kind, details)
        return {"type": kind, "ok": False, "order": replacement, **submitted, "error": details["message"]}

    rollback = dict(plan["previousOrder"])
    original_cli = str(target.get("cliOrdId") or "")
    was_managed = original_cli.startswith((MANAGED_TP_PREFIX, MANAGED_SL_PREFIX))
    rollback["cliOrdId"] = _new_protection_client_id(symbol, rollback["orderType"], was_managed, "rollback")
    restored = _submit_protection(ctx, rollback)
    if restored["outcome"] == "confirmed":
        _clear_unprotected(ctx, symbol, alert_kind)
        return {"type": kind, "ok": False, "outcome": "rejected", "rolledBack": True,
                "order": replacement, "rollbackOrder": rollback, "replacement": submitted,
                "rollback": restored, "error": "replacement rejected; previous protection restored"}
    details = {
        "message": f"UNPROTECTED: replacement rejected and rollback {restored['outcome']}",
        "replacementError": submitted.get("error"), "rollbackError": restored.get("error"),
    }
    _set_unprotected(ctx, symbol, alert_kind, details)
    return {"type": kind, "ok": False, "outcome": "unknown", "unprotected": True,
            "order": replacement, "rollbackOrder": rollback, "replacement": submitted,
            "rollback": restored, "error": details["message"]}


def managed_protection_sync_actions(
    positions: list[dict[str, Any]], orders: list[dict[str, Any]],
    managed_order_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Resize app-managed full-position TP/SL orders after the position changes."""
    positions = _checked_rows(positions, "positions")
    orders = _checked_rows(orders, "open orders")
    managed_order_ids = managed_order_ids or set()
    by_symbol = {str(p.get("symbol")): p for p in positions if p.get("symbol") and _dec(p.get("size")) > 0}
    actions = []
    for order in orders:
        cli_id = str(order.get("cliOrdId") or "")
        order_id = str(order.get("order_id") or order.get("orderId") or "")
        order_type = str(order.get("orderType") or "").lower()
        if cli_id.startswith(MANAGED_TP_PREFIX) or (order_id in managed_order_ids and order_type in PROTECTION_ORDER_TYPES["TP"]):
            action_type, kind = "replace_tp", "TP"
        elif cli_id.startswith(MANAGED_SL_PREFIX) or (order_id in managed_order_ids and order_type in {"stp", "stop"}):
            action_type, kind = "replace_sl", "SL"
        else:
            continue
        symbol = str(order.get("symbol") or "")
        position = by_symbol.get(symbol)
        if not position or str(order.get("reduceOnly")).lower() != "true":
            continue
        peers = [
            candidate for candidate in orders
            if str(candidate.get("symbol") or "") == symbol and is_protection_order(candidate, kind)
        ]
        if len(peers) != 1:  # never rewrite intentional partial ladders or ambiguous protection
            continue
        current_size = _dec(position.get("size"))
        order_size = _dec(order.get("unfilledSize") if order.get("unfilledSize") is not None else order.get("size"))
        stop_price = protection_price(order)
        if current_size == order_size or stop_price is None:
            continue
        actions.append({
            "type": action_type,
            "symbol": symbol,
            "stopPrice": stop_price,
            "managed": True,
            "syncFromSize": _fmt(order_size),
            "syncToSize": _fmt(current_size),
            "orderId": order_id,
            "sourceCliOrdId": cli_id,
        })
    return actions


def _cancel_ids(a: dict[str, Any], ctx: ActionContext, symbol_only: bool) -> list[dict[str, str]]:
    if symbol_only:
        symbol = str(a.get("symbol") or "").strip().upper()
        if not symbol.startswith("PF_"):
            raise ActionError("PF_ symbol required")
        orders = [
            o for o in _checked_rows(ctx.get_orders(), "open orders")
            if isinstance(o, dict) and str(o.get("symbol")) == symbol
        ]
        if a.get("side"):
            orders = [o for o in orders if str(o.get("side")).lower() == str(a["side"]).lower()]
        if a.get("excludeReduceOnly"):
            orders = [o for o in orders if str(o.get("reduceOnly")).lower() != "true"]
        elif "reduceOnly" in a and a.get("reduceOnly") is not None:
            want = str(bool(a.get("reduceOnly"))).lower()
            orders = [o for o in orders if str(o.get("reduceOnly")).lower() == want]
    else:
        # /cancelorder accepts cliOrdId or order_id — normalize whatever key the caller used
        if a.get("cliOrdId"):
            return [{"cliOrdId": str(a["cliOrdId"])}]
        oid = a.get("orderId") or a.get("order_id")
        if not oid:
            raise ActionError("cliOrdId or orderId required")
        return [{"order_id": str(oid)}]
    ids = []
    for o in orders:
        if o.get("cliOrdId"):
            ids.append({"cliOrdId": str(o["cliOrdId"])})
        elif o.get("order_id") or o.get("orderId"):
            ids.append({"order_id": str(o.get("order_id") or o.get("orderId"))})
    return ids


def _aggregate_outcome(results: list[dict[str, Any]]) -> str:
    outcomes = [result.get("outcome") for result in results]
    if outcomes and all(outcome == "confirmed" for outcome in outcomes):
        return "confirmed"
    if any(outcome == "confirmed" for outcome in outcomes):
        return "partial"
    if any(outcome == "unknown" for outcome in outcomes):
        return "unknown"
    return "rejected"


def _dispatch(a: dict[str, Any], ctx: ActionContext, armed: bool) -> dict[str, Any]:
    kind = str(a.get("type") or "").strip().lower()

    if kind == "order":
        params = _validate_order(a, ctx)
        params.pop("cliOrdId", None)  # every submission gets a fresh server-generated ID
        if not armed:
            return {"type": kind, "ok": True, "outcome": "simulated", "simulated": True, "order": params}
        if not params.get("reduceOnly"):
            ctx.require_new_exposure(params["symbol"])
        submitted = submit_one(ctx, params, f"kt-order-{params['symbol']}")
        return {"type": kind, "ok": submitted["outcome"] == "confirmed", "order": submitted["params"], **submitted}

    if kind == "ladder":
        plan = _ladder_plan(a, ctx)
        if "previewHash" in plan:
            if a.get("previewHash") and a["previewHash"] != plan["previewHash"]:
                raise ActionError("Grid changed since preview. Preview again before submitting.")
            try:
                plan["warnings"] += validate_grid(plan, ctx)
            except GridError as exc:
                raise ActionError(str(exc)) from exc
        if not armed:
            return {"type": kind, "ok": True, "outcome": "simulated", "simulated": True, **plan}
        if not a.get("reduceOnly"):
            ctx.require_new_exposure(plan["symbol"])
        responses = []
        for index, order in enumerate(plan["orders"], 1):
            try:
                if "previewHash" in plan:
                    if order.get("reduceOnly"):
                        ctx.fresh_ticker(plan["symbol"])
                    else:
                        ctx.require_new_exposure(plan["symbol"])
                submitted = submit_one(ctx, order, f"kt-ladder-{plan['symbol']}-{index}")
            except Exception as exc:
                submitted = {"params": order, "outcome": "rejected" if isinstance(exc, ActionError) else "unknown",
                             "error": f"Grid stopped: {exc}"}
            responses.append({"order": submitted["params"], **submitted})
            if submitted["outcome"] != "confirmed":
                responses.extend({
                    "outcome": "rejected", "error": "not executed after earlier ladder failure",
                    "order": pending,
                } for pending in plan["orders"][index:])
                break
        outcome = _aggregate_outcome(responses)
        return {
            "type": kind, "ok": outcome == "confirmed", "outcome": outcome, **plan, "responses": responses,
            **({"error": f"ladder outcome {outcome}: {sum(r['outcome'] == 'confirmed' for r in responses)}/{len(responses)} confirmed"}
               if outcome != "confirmed" else {}),
        }

    if kind == "close":
        if a.get("allowFlat"):
            symbol = str(a.get("symbol") or "").strip().upper()
            positions = _checked_rows(ctx.get_positions(), "positions")
            position = next((row for row in positions if str(row.get("symbol")) == symbol), None)
            if not position or _dec(position.get("size") or 0) <= 0:
                return {"type": kind, "ok": True, "outcome": "confirmed", "noOp": True,
                        "note": f"{symbol} is already flat"}
        params = _close_plan(a, ctx)
        if not armed:
            return {"type": kind, "ok": True, "outcome": "simulated", "simulated": True, **params}
        submitted = submit_one(ctx, params, f"kt-close-{params['symbol']}")
        return {"type": kind, "ok": submitted["outcome"] == "confirmed", **params, "order": submitted["params"], **submitted}

    if kind in {"replace_tp", "replace_sl"}:
        order_type = "take_profit" if kind == "replace_tp" else "stp"
        plan = _replace_protection_plan(a, ctx, order_type)
        if not armed:
            return {"type": kind, "ok": True, "outcome": "simulated", "simulated": True, **plan}
        return _execute_protection_change(kind, plan, ctx)

    if kind == "cancel_all_orders":
        orders = _checked_rows(ctx.get_orders(), "open orders")
        invalid = next(
            (str(order.get("symbol") or "<missing>") for order in orders
             if not str(order.get("symbol") or "").startswith("PF_")),
            None,
        )
        if invalid:
            raise ActionError(f"cannot cancel unsupported order {invalid}")
        ids = []
        seen = set()
        for order in orders:
            target = ({"cliOrdId": str(order["cliOrdId"])} if order.get("cliOrdId") else
                      {"order_id": str(order.get("order_id") or order.get("orderId"))}
                      if order.get("order_id") or order.get("orderId") else None)
            if target is None:
                raise ActionError(f"open order on {order.get('symbol')} is missing an order ID")
            key = tuple(target.items())
            if key not in seen:
                seen.add(key)
                ids.append(target)
        if not armed:
            return {"type": kind, "ok": True, "outcome": "simulated", "simulated": True,
                    "cancelOrderIds": ids, **({"noOp": True} if not ids else {})}
        if not ids:
            return {"type": kind, "ok": True, "outcome": "confirmed", "noOp": True, "results": []}
        results = [cancel_one(ctx, target) for target in ids]
        outcome = _aggregate_outcome(results)
        return {
            "type": kind, "ok": outcome == "confirmed", "outcome": outcome, "results": results,
            **({"error": f"cancel-all outcome {outcome}: {sum(r['outcome'] == 'confirmed' for r in results)}/{len(results)} confirmed"}
               if outcome != "confirmed" else {}),
        }

    if kind == "cancel_all":
        ids = _cancel_ids(a, ctx, symbol_only=True)
        if not armed:
            return {"type": kind, "ok": True, "outcome": "simulated", "simulated": True, "cancelOrderIds": ids}
        if not ids:
            return {"type": kind, "ok": True, "outcome": "confirmed", "noOp": True, "results": []}
        results = [cancel_one(ctx, target) for target in ids]
        outcome = _aggregate_outcome(results)
        return {
            "type": kind, "ok": outcome == "confirmed", "outcome": outcome, "results": results,
            **({"error": f"cancel-all outcome {outcome}: {sum(r['outcome'] == 'confirmed' for r in results)}/{len(results)} confirmed"}
               if outcome != "confirmed" else {}),
        }

    if kind == "cancel":
        ids = _cancel_ids(a, ctx, symbol_only=False)
        if not armed:
            return {"type": kind, "ok": True, "outcome": "simulated", "simulated": True, "cancelOrderIds": ids}
        results = [cancel_one(ctx, target) for target in ids]
        outcome = _aggregate_outcome(results)
        return {
            "type": kind, "ok": outcome == "confirmed", "outcome": outcome, "results": results,
            **({"error": results[0].get("error") or f"cancel outcome {outcome}"} if outcome != "confirmed" else {}),
        }

    if kind == "chase":
        symbol = str(a.get("symbol") or "").strip().upper()
        side = str(a.get("side") or "").strip().lower()
        size = _dec(a.get("size"))
        if not symbol.startswith("PF_") or side not in {"buy", "sell"}:
            raise ActionError("PF_ symbol and side (buy|sell) required")
        if size <= 0:
            raise ActionError("size must be positive")
        if a.get("closePosition"):
            if not a.get("reduceOnly"):
                raise ActionError("closing Chase must be reduce-only")
            positions = _checked_rows(ctx.get_positions(), "positions")
            position = next((row for row in positions if str(row.get("symbol")) == symbol), None)
            if not position or _dec(position.get("size") or 0) <= 0:
                return {"type": kind, "ok": True, "outcome": "confirmed", "noOp": True,
                        "note": f"{symbol} is already flat"}
            position_side = str(position.get("side") or "").lower()
            expected_side = "sell" if position_side == "long" else "buy" if position_side == "short" else ""
            if not expected_side or side != expected_side:
                raise ActionError(f"closing Chase side does not match current {symbol} position")
            size = min(size, _dec(position.get("size")))
        if ctx.chase is None:
            raise ActionError("chase engine unavailable")
        spec = {
            "symbol": symbol, "side": side, "size": float(size),
            "reduceOnly": bool(a.get("reduceOnly")),
            "timeoutSec": float(a.get("timeoutSec") or 300),
            "maxRepegs": int(a.get("maxRepegs") or 120),
            "repegSec": max(1.0, float(a.get("repegSec") or 5)),
            "offsetTicks": max(0, int(a.get("offsetTicks") or 0)),
        }
        if not armed:
            t = ctx.hub.ticker(symbol) or ctx.get_ticker_rest(symbol) or {}
            return {"type": kind, "ok": True, "outcome": "simulated", "simulated": True,
                    "spec": spec,
                    "note": f"would peg {'best bid' if side == 'buy' else 'best ask'} (now {t.get('bid') if side == 'buy' else t.get('ask')}) and re-peg every {spec['repegSec']}s up to {spec['timeoutSec']}s"}
        starter = getattr(ctx, "start_chase", None)
        snap = starter(spec) if starter else ctx.chase.start(spec, ctx)
        return {"type": kind, "ok": True, "outcome": "confirmed", "chase": snap,
                "note": f"chase {snap['id']} running; fills arrive as notifications"}

    raise ActionError(f"unknown action type {kind!r}")


def normalize_actions(actions: Any, max_actions: int | None = 25) -> list[dict[str, Any]]:
    """Normalize the one safe model slip and reject a malformed batch before dispatch."""
    if not isinstance(actions, list) or not actions:
        raise ActionError("actions must be a non-empty list")
    if max_actions is not None and len(actions) > max_actions:
        raise ActionError(f"max {max_actions} actions per request")

    normalized = []
    for index, raw in enumerate(actions, 1):
        if not isinstance(raw, dict):
            raise ActionError(f"action {index} must be an object")
        action = dict(raw)
        kind = str(action.get("type") or "").strip().lower()
        if not kind and str(action.get("orderType") or "").strip():
            kind = "order"
        if kind not in ACTION_TYPES:
            raise ActionError(f"action {index} has unknown action type {kind!r}")
        action["type"] = kind
        normalized.append(action)
    return normalized


def build_flatten_actions(mode: str, positions: Any, orders: Any = None, *, symbol: str = "",
                          defer_order_validation: bool = False) -> list[dict[str, Any]]:
    """Build a server-authoritative all-position exit plan without side effects."""
    if mode not in {"emergency", "chase"}:
        raise ActionError("flatten mode must be emergency or chase")
    if symbol and (mode != "chase" or not symbol.startswith("PF_")):
        raise ActionError("symbol-scoped flatten requires chase mode and a PF_ symbol")
    if not isinstance(positions, list):
        raise ActionError("position state unavailable; flatten rejected")
    failed = next((str(row.get("error")) for row in positions if isinstance(row, dict) and row.get("error")), None)
    if failed:
        raise ActionError(f"position state unavailable; flatten rejected: {failed}")
    live: list[tuple[dict[str, Any], Decimal]] = []
    for row in positions:
        if not isinstance(row, dict):
            raise ActionError("position state unavailable; flatten rejected: malformed row")
        size = _dec(row.get("size") or 0)
        if size <= 0:
            continue
        position_symbol = str(row.get("symbol") or "")
        side = str(row.get("side") or "").lower()
        if not position_symbol.startswith("PF_"):
            raise ActionError(f"cannot flatten unsupported position {position_symbol or '<missing>'}")
        if side not in {"long", "short"}:
            raise ActionError(f"cannot flatten {position_symbol}: position side is unavailable")
        live.append((row, size))

    actions = [{
        "type": "close" if mode == "emergency" else "chase",
        "symbol": str(row["symbol"]),
        **({"side": "sell" if str(row["side"]).lower() == "long" else "buy",
            "size": float(size), "reduceOnly": True, "closePosition": True}
           if mode == "chase" else {"percent": 100, "allowFlat": True}),
    } for row, size in live if not symbol or row["symbol"] == symbol]
    if mode == "chase":
        return actions
    if defer_order_validation:
        # Live emergency closes need positions, not pending-order data. Validate orders at cancellation.
        return actions + [{"type": "cancel_all_orders"}]

    if not isinstance(orders, list):
        raise ActionError("order state unavailable; emergency flatten rejected")
    failed = next((str(row.get("error")) for row in orders if isinstance(row, dict) and row.get("error")), None)
    if failed:
        raise ActionError(f"order state unavailable; emergency flatten rejected: {failed}")
    if any(not isinstance(row, dict) for row in orders):
        raise ActionError("order state unavailable; emergency flatten rejected: malformed row")
    invalid = next(
        (str(row.get("symbol") or "<missing>") for row in orders if not str(row.get("symbol") or "").startswith("PF_")),
        None,
    )
    if invalid:
        raise ActionError(f"cannot cancel unsupported order {invalid}")
    actions.append({"type": "cancel_all_orders"})
    return actions


def execute_actions(
    actions: list[dict[str, Any]], ctx: ActionContext, armed: bool, *, max_actions: int | None = 25,
) -> list[dict[str, Any]]:
    normalized = normalize_actions(actions, max_actions=max_actions)  # structural preflight: no partial batch on bad envelopes
    results = []
    for index, a in enumerate(normalized):
        try:
            result = _dispatch(a, ctx, armed)
        except ActionError as exc:
            result = {"type": a["type"], "ok": False, "outcome": "rejected", "error": str(exc)}
        except KrakenFuturesError as exc:
            result = {"type": a["type"], "ok": False, "outcome": "unknown", "error": str(exc)}
        except Exception as exc:  # never kill the connection mid-batch
            result = {"type": a["type"], "ok": False, "outcome": "unknown", "error": f"internal: {type(exc).__name__}: {exc}"}
        results.append(result)
        after_action = getattr(ctx, "after_action", None)
        if after_action:
            try:
                after_action(a, result, armed)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ActionError) else f"state refresh failed: {type(exc).__name__}: {exc}"
                result["stateRefreshError"] = reason
                for pending in normalized[index + 1:]:
                    results.append({"type": pending["type"], "ok": False, "outcome": "rejected", "error": f"not executed: {reason}"})
                break
        if armed and result.get("outcome") != "confirmed":
            reason = f"prior action outcome {result.get('outcome') or 'unknown'}"
            for pending in normalized[index + 1:]:
                results.append({"type": pending["type"], "ok": False, "outcome": "rejected", "error": f"not executed: {reason}"})
            break
    return results


def execute_emergency_flatten(
    actions: list[dict[str, Any]], ctx: ActionContext, stop_chases: Callable[[], dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Close every position, then stop Chase workers and cancel the current global order set."""
    close_actions = [action for action in actions if action["type"] == "close"]
    cancel_action = next(action for action in actions if action["type"] == "cancel_all_orders")
    results = execute_actions(close_actions, ctx, True, max_actions=None) if close_actions else []
    failed_close = next(
        (result for result in results if result.get("outcome") != "confirmed" or result.get("stateRefreshError")),
        None,
    )
    aborting = {"requested": [], "completed": [], "pending": []}
    if failed_close:
        reason = failed_close.get("stateRefreshError") or failed_close.get("error") or "close was not confirmed"
        results.append({"type": "cancel_all_orders", "ok": False, "outcome": "rejected",
                        "error": f"not executed: {reason}"})
        return results, aborting
    try:
        aborting = stop_chases()
        unsafe = [item for item in aborting["completed"] if item.get("status") == "unknown"]
        if aborting["pending"] or unsafe:
            raise ActionError("existing Chase orders could not be reconciled")
    except Exception as exc:
        results.append({"type": "cancel_all_orders", "ok": False, "outcome": "unknown",
                        "error": f"not executed: Chase shutdown failed: {type(exc).__name__}: {exc}"})
        return results, aborting
    results.extend(execute_actions([cancel_action], ctx, True, max_actions=None))
    return results, aborting


ACTIONS_PROMPT = """Trade actions use the schemas below.

Execution routing:
- When the user explicitly asks to open, close, cancel, resize, or move an order, use the matching direct write tool. ARMED means real execution; DISARMED means simulation.
- Use `propose_actions` only when the user explicitly asks for a draft, plan, proposal, or review card.
- Chase is the exception: it always remains proposal-only and must go through `propose_actions` for human review.
- Never describe a direct tool call as a card. Report its returned status exactly: live success, simulation, or failure.

Action schemas:

- {"type": "order", "symbol", "side": "buy|sell", "orderType": "mkt|lmt|post|ioc|stp|take_profit", "size", "limitPrice?" (required for lmt/post/ioc), "stopPrice?" (required for stp/take_profit), "reduceOnly?"}
- {"type": "ladder", "symbol", "side", "notional" (USD total), "orders" (1-20), "depthPercent", "orderType?": "post|lmt" (default post), "includeCurrent?" (first rung at current price), "reduceOnly?"}
- {"type": "close", "symbol", "percent?" (1-100, default 100) or "size"} — reduce-only market close
- {"type": "replace_tp", "symbol", "stopPrice", "orderId?"} — edits the exact TP when an ID is supplied; otherwise edits the only unambiguous TP or creates one managed full-position MAKER TP: a post-only reduce-only limit at stopPrice (it must rest beyond the best bid for a long, below the best ask for a short; it fills only if price trades through it, at the maker fee). An older mark-trigger TP is replaced by a maker TP.
- {"type": "replace_sl", "symbol", "stopPrice", "orderId?"} — edits the exact SL when an ID is supplied; otherwise edits the only unambiguous SL or creates one managed full-position mark-trigger SL
- {"type": "cancel_all", "symbol", "side?": "buy|sell", "reduceOnly?": true|false, "excludeReduceOnly?"} — filtered cancel. Examples: {"symbol","side":"sell","reduceOnly":true} clears exactly the TP/scale-out sells; {"symbol","excludeReduceOnly":true} clears entry grids while keeping protection live.
- {"type": "cancel", "cliOrdId"} or {"orderId"}
- {"type": "chase", "symbol", "side", "size", "timeoutSec?" (default 300), "repegSec?" (default 5), "maxRepegs?", "offsetTicks?"} — POST-ONLY maker chase: rests at best bid (buy) / best ask (sell), re-pegs as the market moves, until filled/timeout. Use when the user wants in WITHOUT taker fees. Requires the terminal ARMED; the action returns "chase started" immediately, fills arrive as notifications afterwards.

THE OPEN ORDERS LIST IN THE SNAPSHOT IS YOUR BOOK STATE. Before any action that adds orders, reconcile against it:
1. RESIZE/MOVE protection by exact order ID; the server uses `editorder` so the working order is never canceled first (converting an older trigger TP to a maker TP is a cancel then replace). Never reconstruct IDs from memory or collapse a partial ladder.
2. NEVER stack new orders on top of existing ones at the same or overlapping prices. One order per price level.
3. NEVER end a proposal with "let me know if you also want X cleared/replaced" — include the cleanup in the block or state explicitly that nothing needs cleaning.
4. Reduce-only sells must not exceed the current position size in total. If the position grew, recompute per-level sizes from the snapshot's CURRENT size; if it shrank, trim levels.
5. Do not batch a resting post/lmt entry with a reduce-only TP for the hoped-for fill. The entry may remain unfilled, so propose the entry first and place its TP only after the position exists (unless the current position already covers that TP size).
6. Reduce-only orders self-cap at the position size at execution, but the book should still match intent.

Numbers:
- Compute every number yourself from the snapshot (entry, mark, position size, available margin). "Move my TP to +8%" means you emit the price.
- ROUND ALL PRICES TO THE SYMBOL'S tickSize (in SYMBOL META). A price off-tick gets adjusted by the server or rejected by Kraken.
- SIZES ARE IN CONTRACTS. futures_inverse (PI_*): 1 contract = contractSize USD notional (contractSize 1 => $500 = size 500, price-independent). flexible_futures (PF_*): 1 contract = 1 unit of underlying, notional = size × price ($500 of PF_TRUMPUSD at $2.80 => size 178.6).
- Entry orders that exceed available margin get rejected by Kraken; size entry grids against availableMargin.
- Ladder (buy grids build down from current price, sell grids build up) is for uniform grids from a price anchor; use individual "order" actions for custom price levels like scaling out across a fixed range.

Output:
- For direct execution, call the write tool first, then state the returned live/simulated/failed result. Do not mention a card.
- For an explicit proposal request, give a short preamble and call `propose_actions` with the complete action array.
- If the request is ambiguous in a way that changes risk (e.g. which grid, which range), ask before using either route.
"""
