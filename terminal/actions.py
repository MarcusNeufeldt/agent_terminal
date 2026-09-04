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
  {"type": "cancel_all", "symbol"}
  {"type": "cancel",     "cliOrdId"}
"""

from __future__ import annotations

import json
import time
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Callable

from kraken_client import KrakenFuturesError

ACTION_TYPES = {"order", "ladder", "close", "replace_tp", "replace_sl", "cancel_all", "cancel", "chase"}
ORDER_TYPES = {"mkt", "lmt", "post", "ioc", "stp", "take_profit"}
LIMIT_TYPES = {"lmt", "post", "ioc"}
TRIGGER_TYPES = {"stp", "take_profit"}
MANAGED_TP_PREFIX = "kt-full-tp-"
MANAGED_SL_PREFIX = "kt-full-sl-"


class ActionError(Exception):
    pass


def _check_kraken(response: Any) -> Any:
    """Kraken signals failures two ways: result != success, or result == success
    with a rejecting sendStatus (e.g. status "postWouldExecute" on post-only)."""
    if isinstance(response, dict):
        send = response.get("sendStatus") or {}
        status = str(send.get("status") or "")
        if str(response.get("result")) != "success" or (status and status != "placed"):
            detail = json.dumps(send or response.get("cancelStatus") or response)
            raise ActionError(f"Kraken error: {detail[:300]}")
    return response


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


class ActionContext:
    """Everything the handlers need; supplied by server.py."""

    def __init__(
        self,
        *,
        client,
        hub,
        get_positions: Callable[[], list[dict]],
        get_orders: Callable[[], list[dict]],
        get_instruments: Callable[[], dict],
        get_ticker_rest: Callable[[str], dict | None],
        chase: Any = None,
    ) -> None:
        self.client = client
        self.hub = hub
        self.get_positions = get_positions
        self.get_orders = get_orders
        self.get_instruments = get_instruments
        self.get_ticker_rest = get_ticker_rest
        self.chase = chase
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

    def current_price(self, symbol: str) -> Decimal:
        ticker = self.hub.ticker(symbol) or self.get_ticker_rest(symbol) or {}
        price = ticker.get("last") or ticker.get("markPrice")
        if not price:
            raise ActionError(f"no current price available for {symbol}")
        return _dec(price)

    def mark_price(self, symbol: str) -> Decimal:
        ticker = self.hub.ticker(symbol) or self.get_ticker_rest(symbol) or {}
        price = ticker.get("markPrice")
        if not price:
            raise ActionError(f"no mark price available for {symbol}")
        return _dec(price)


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
    if not symbol:
        raise ActionError("symbol required")
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
    symbol = str(a.get("symbol") or "").strip().upper()
    side = str(a.get("side") or "").strip().lower()
    if not symbol:
        raise ActionError("symbol required")
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
    if not symbol:
        raise ActionError("symbol required")
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


def _replace_protection_plan(a: dict[str, Any], ctx: ActionContext, order_type: str) -> dict[str, Any]:
    symbol = str(a.get("symbol") or "").strip().upper()
    if not symbol:
        raise ActionError("symbol required")
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
    mark = ctx.mark_price(symbol)
    must_be_above = (side_pos == "long") == (order_type == "take_profit")
    if stop == mark or (stop > mark) != must_be_above:
        label = "take profit" if order_type == "take_profit" else "stop loss"
        direction = "above" if must_be_above else "below"
        raise ActionError(f"{label} price must be {direction} current mark {mark}")

    orders = _checked_rows(ctx.get_orders(), "open orders")
    existing = [
        o for o in orders
        if isinstance(o, dict)
        and str(o.get("symbol")) == symbol
        and str(o.get("orderType") or "").lower() in ({"take_profit"} if order_type == "take_profit" else {"stp", "stop"})
        and str(o.get("reduceOnly")).lower() == "true"
    ]
    to_cancel = []
    for o in existing:
        if o.get("cliOrdId"):
            to_cancel.append({"cliOrdId": str(o["cliOrdId"])})
        elif o.get("order_id") or o.get("orderId"):
            to_cancel.append({"order_id": str(o.get("order_id") or o.get("orderId"))})

    size_total = _dec(position.get("size"))
    order_input = {
        "orderType": order_type,
        "symbol": symbol,
        "side": "sell" if side_pos == "long" else "buy",
        "size": _fmt(size_total),
        "stopPrice": _fmt(stop),
        "triggerSignal": "mark",
        "reduceOnly": True,
    }
    if a.get("managed", True):
        prefix = MANAGED_TP_PREFIX if order_type == "take_profit" else MANAGED_SL_PREFIX
        order_input["cliOrdId"] = f"{prefix}{symbol}-{int(time.time() * 1000)}"
    new_order = _validate_order(order_input, ctx)
    return {"cancelOrderIds": to_cancel, "order": new_order}


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
        if cli_id.startswith(MANAGED_TP_PREFIX) or (order_id in managed_order_ids and order_type == "take_profit"):
            action_type, order_types = "replace_tp", {"take_profit"}
        elif cli_id.startswith(MANAGED_SL_PREFIX) or (order_id in managed_order_ids and order_type in {"stp", "stop"}):
            action_type, order_types = "replace_sl", {"stp", "stop"}
        else:
            continue
        symbol = str(order.get("symbol") or "")
        position = by_symbol.get(symbol)
        if not position or str(order.get("reduceOnly")).lower() != "true":
            continue
        peers = [
            candidate for candidate in orders
            if str(candidate.get("symbol") or "") == symbol
            and str(candidate.get("orderType") or "").lower() in order_types
            and str(candidate.get("reduceOnly")).lower() == "true"
        ]
        if len(peers) != 1:  # never rewrite intentional partial ladders or ambiguous protection
            continue
        current_size = _dec(position.get("size"))
        order_size = _dec(order.get("unfilledSize") if order.get("unfilledSize") is not None else order.get("size"))
        stop_price = order.get("stopPrice")
        if current_size == order_size or stop_price is None:
            continue
        actions.append({
            "type": action_type,
            "symbol": symbol,
            "stopPrice": stop_price,
            "managed": True,
            "syncFromSize": _fmt(order_size),
            "syncToSize": _fmt(current_size),
            "sourceCliOrdId": cli_id,
        })
    return actions


def _cancel_ids(a: dict[str, Any], ctx: ActionContext, symbol_only: bool) -> list[dict[str, str]]:
    if symbol_only:
        symbol = str(a.get("symbol") or "").strip().upper()
        if not symbol:
            raise ActionError("symbol required")
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
    if not ids:
        raise ActionError("no matching open orders")
    return ids


def _dispatch(a: dict[str, Any], ctx: ActionContext, armed: bool) -> dict[str, Any]:
    kind = str(a.get("type") or "").strip().lower()

    if kind == "order":
        params = _validate_order(a, ctx)
        if not armed:
            return {"type": kind, "ok": True, "simulated": True, "order": params}
        return {"type": kind, "ok": True, "order": params,
                "response": _check_kraken(ctx.client.post("/sendorder", params=params, private=True))}

    if kind == "ladder":
        plan = _ladder_plan(a, ctx)
        if not armed:
            return {"type": kind, "ok": True, "simulated": True, **plan}
        responses = []
        for order in plan["orders"]:
            try:
                responses.append({
                    "order": order,
                    "response": _check_kraken(ctx.client.post("/sendorder", params=order, private=True)),
                })
            except (KrakenFuturesError, ActionError) as exc:
                responses.append({"order": order, "error": str(exc)})
        return {"type": kind, "ok": True, **plan, "responses": responses}

    if kind == "close":
        params = _close_plan(a, ctx)
        if not armed:
            return {"type": kind, "ok": True, "simulated": True, **params}
        return {"type": kind, "ok": True, **params,
                "response": _check_kraken(ctx.client.post("/sendorder", params=params, private=True))}

    if kind in {"replace_tp", "replace_sl"}:
        order_type = "take_profit" if kind == "replace_tp" else "stp"
        plan = _replace_protection_plan(a, ctx, order_type)
        if not armed:
            return {"type": kind, "ok": True, "simulated": True, **plan}
        cancel_results = []
        for target in plan["cancelOrderIds"]:
            try:
                cancel_results.append({
                    "target": target,
                    "response": _check_kraken(ctx.client.post("/cancelorder", params=target, private=True)),
                })
            except (KrakenFuturesError, ActionError) as exc:
                cancel_results.append({"target": target, "error": str(exc)})
        if any(result.get("error") for result in cancel_results):
            return {"type": kind, "ok": False, "error": "existing protection order could not be canceled", "cancelResults": cancel_results}
        return {"type": kind, "ok": True,
                "cancelResults": cancel_results,
                "order": plan["order"],
                "response": _check_kraken(ctx.client.post("/sendorder", params=plan["order"], private=True))}

    if kind == "cancel_all":
        ids = _cancel_ids(a, ctx, symbol_only=True)
        if not armed:
            return {"type": kind, "ok": True, "simulated": True, "cancelOrderIds": ids}
        results = []
        for target in ids:
            try:
                results.append({
                    "target": target,
                    "response": _check_kraken(ctx.client.post("/cancelorder", params=target, private=True)),
                })
            except (KrakenFuturesError, ActionError) as exc:
                results.append({"target": target, "error": str(exc)})
        return {"type": kind, "ok": True, "results": results}

    if kind == "cancel":
        ids = _cancel_ids(a, ctx, symbol_only=False)
        if not armed:
            return {"type": kind, "ok": True, "simulated": True, "cancelOrderIds": ids}
        results = []
        for target in ids:
            try:
                results.append({
                    "target": target,
                    "response": _check_kraken(ctx.client.post("/cancelorder", params=target, private=True)),
                })
            except (KrakenFuturesError, ActionError) as exc:
                results.append({"target": target, "error": str(exc)})
        return {"type": kind, "ok": True, "results": results}

    if kind == "chase":
        symbol = str(a.get("symbol") or "").strip().upper()
        side = str(a.get("side") or "").strip().lower()
        size = _dec(a.get("size"))
        if not symbol or side not in {"buy", "sell"}:
            raise ActionError("symbol and side (buy|sell) required")
        if size <= 0:
            raise ActionError("size must be positive")
        if ctx.chase is None:
            raise ActionError("chase engine unavailable")
        spec = {
            "symbol": symbol, "side": side, "size": float(size),
            "timeoutSec": float(a.get("timeoutSec") or 300),
            "maxRepegs": int(a.get("maxRepegs") or 120),
            "repegSec": max(1.0, float(a.get("repegSec") or 5)),
            "offsetTicks": max(0, int(a.get("offsetTicks") or 0)),
        }
        if not armed:
            t = ctx.hub.ticker(symbol) or ctx.get_ticker_rest(symbol) or {}
            return {"type": kind, "ok": True, "simulated": True,
                    "spec": spec,
                    "note": f"would peg {'best bid' if side == 'buy' else 'best ask'} (now {t.get('bid') if side == 'buy' else t.get('ask')}) and re-peg every {spec['repegSec']}s up to {spec['timeoutSec']}s"}
        snap = ctx.chase.start(spec, ctx)
        return {"type": kind, "ok": True, "chase": snap,
                "note": f"chase {snap['id']} running — fills arrive as notifications"}

    raise ActionError(f"unknown action type {kind!r}")


def normalize_actions(actions: Any) -> list[dict[str, Any]]:
    """Normalize the one safe model slip and reject a malformed batch before dispatch."""
    if not isinstance(actions, list) or not actions:
        raise ActionError("actions must be a non-empty list")
    if len(actions) > 25:
        raise ActionError("max 25 actions per request")

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


def execute_actions(actions: list[dict[str, Any]], ctx: ActionContext, armed: bool) -> list[dict[str, Any]]:
    normalized = normalize_actions(actions)  # structural preflight: no partial batch on bad envelopes
    results = []
    for index, a in enumerate(normalized):
        try:
            result = _dispatch(a, ctx, armed)
        except ActionError as exc:
            result = {"type": a["type"], "ok": False, "error": str(exc)}
        except KrakenFuturesError as exc:
            result = {"type": a["type"], "ok": False, "error": str(exc)}
        except Exception as exc:  # never kill the connection mid-batch
            result = {"type": a["type"], "ok": False, "error": f"internal: {type(exc).__name__}: {exc}"}
        results.append(result)
        after_action = getattr(ctx, "after_action", None)
        if after_action:
            try:
                after_action(a, result, armed)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ActionError) else f"state refresh failed: {type(exc).__name__}: {exc}"
                result["stateRefreshError"] = reason
                for pending in normalized[index + 1:]:
                    results.append({"type": pending["type"], "ok": False, "error": f"not executed: {reason}"})
                break
    return results


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
- {"type": "replace_tp", "symbol", "stopPrice"} — cancels existing reduce-only TP triggers and places one managed mark-trigger TP for the full position; that single managed TP follows later position-size changes
- {"type": "replace_sl", "symbol", "stopPrice"} — cancels existing reduce-only stop-loss triggers and places one managed mark-trigger stop for the full position; that single managed SL follows later position-size changes
- {"type": "cancel_all", "symbol", "side?": "buy|sell", "reduceOnly?": true|false, "excludeReduceOnly?"} — filtered cancel. Examples: {"symbol","side":"sell","reduceOnly":true} clears exactly the TP/scale-out sells; {"symbol","excludeReduceOnly":true} clears entry grids while keeping protection live.
- {"type": "cancel", "cliOrdId"} or {"orderId"}
- {"type": "chase", "symbol", "side", "size", "timeoutSec?" (default 300), "repegSec?" (default 5), "maxRepegs?", "offsetTicks?"} — POST-ONLY maker chase: rests at best bid (buy) / best ask (sell), re-pegs as the market moves, until filled/timeout. Use when the user wants in WITHOUT taker fees. Requires the terminal ARMED; the action returns "chase started" immediately, fills arrive as notifications afterwards.

THE OPEN ORDERS LIST IN THE SNAPSHOT IS YOUR BOOK STATE. Before any action that adds orders, reconcile against it:
1. RESIZE/MOVE existing orders = cancel the exact old ones (use cancel_all filters, never reconstructed IDs from memory) + place replacements IN THE SAME BLOCK. Proposing a 6-action plan is correct; proposing 11 actions that tear down protection and rebuild it from memory is wrong.
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
