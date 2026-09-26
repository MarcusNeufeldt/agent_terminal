"""Reconciled post-only limit Chase engine.

The engine never treats an order that is merely absent from open orders as
filled, and never replaces an order before confirmed cancellation and fill
reconciliation. Exchange-initiated cancellations stop the worker without replacement.
"""

from __future__ import annotations

import threading
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

from exchange_ops import parse_operation


class ChaseError(Exception):
    pass


class ChaseRejected(ChaseError):
    """Kraken returned a definite operation rejection."""


class ChaseTransient(ChaseError):
    """A read failed while the current exchange order should remain untouched."""


class ChaseUnknown(ChaseError):
    """The exchange outcome is uncertain, so no further order may be placed."""


def _round_tick(value: Decimal, tick: Decimal, direction: str) -> Decimal:
    q = value / tick
    return q.to_integral_value(rounding=ROUND_DOWN if direction == "down" else ROUND_UP) * tick


def _round_size(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)


def _operation_status(response: Any, key: str) -> tuple[str, dict[str, Any]]:
    parsed = parse_operation(response, key, "cancelled", ambiguous_statuses=("notFound",))
    if parsed["outcome"] == "unknown":
        raise ChaseUnknown(parsed.get("error") or f"Kraken {key} outcome unknown")
    if parsed["outcome"] != "confirmed":
        raise ChaseRejected(parsed.get("error") or f"Kraken {key} rejected")
    return str(parsed["nestedStatus"]), parsed["detail"]


class ChaseWorker(threading.Thread):
    def __init__(self, spec: dict[str, Any], ctx: Any, publish: Any):
        super().__init__(daemon=True, name=f"chase-{uuid.uuid4().hex[:6]}")
        self.spec = dict(spec)
        self.ctx = ctx
        self.publish = publish
        self.id = uuid.uuid4().hex[:8]
        self.status = "running"
        self.state = "IDLE"
        self.pegs = 0
        self.filled = 0.0
        self.events: list[str] = []
        self.audit: list[dict[str, Any]] = []
        self.started = time.time()
        self.unknown_reason: str | None = None
        self.stop_reason: str | None = None
        self._abort = threading.Event()
        self._active: dict[str, Any] | None = None
        self._base_filled = 0.0

    def abort(self) -> None:
        self._abort.set()

    def run(self) -> None:
        try:
            self._run()
        except ChaseUnknown as exc:
            self._finish("unknown", str(exc))
        except ChaseError as exc:
            self._finish("rejected", str(exc))
        except Exception as exc:
            self._finish("unknown", f"internal {type(exc).__name__}: {exc}")
        self._publish()

    def snapshot(self) -> dict[str, Any]:
        active = self._active or {}
        return {
            "id": self.id,
            "spec": {key: self.spec.get(key) for key in (
                "symbol", "side", "size", "reduceOnly", "timeoutSec", "maxRepegs", "repegSec", "offsetTicks",
            )},
            "symbol": self.spec.get("symbol"),
            "side": self.spec.get("side"),
            "size": self.spec.get("size"),
            "status": self.status,
            "state": self.state,
            "pegs": self.pegs,
            "filled": self.filled,
            "activeCliOrdId": active.get("cliOrdId"),
            "activeOrderId": active.get("orderId"),
            # The resting peg, so the UI can draw it without waiting for an order poll.
            "activePrice": float(active["price"]) if active.get("price") is not None else None,
            "activeSize": float(active["size"]) if active.get("size") is not None else None,
            "unknownReason": self.unknown_reason,
            "stopReason": self.stop_reason,
            "events": self.events[-8:],
            "audit": self.audit[-20:],
            "started": self.started,
        }

    def _publish(self) -> None:
        try:
            self.publish("chase", self.snapshot())
        except Exception:
            pass

    def _log(self, message: str) -> None:
        self.events.append(f"{time.strftime('%H:%M:%S')} {message}")
        if len(self.events) > 200:
            self.events = self.events[-100:]
        self._publish()

    def _audit(self, event: str, **details: Any) -> None:
        self.audit.append({"at": time.time(), "event": event, "state": self.state, **details})
        if len(self.audit) > 200:
            self.audit = self.audit[-100:]

    def _transition(self, state: str, message: str) -> None:
        self.state = state
        self._log(message)

    def _finish(self, status: str, message: str) -> None:
        self.status = status
        self.state = "UNKNOWN" if status == "unknown" else status.upper()
        self.unknown_reason = message if status == "unknown" else None
        self._audit("final", status=status, filled=self.filled, reason=message)
        self._log(f"chase {status}: {message}; filled {self.filled}/{self.spec.get('size')}")

    def _terminal_status(self, fallback: str) -> str:
        if self.filled >= float(self.spec["size"]) - 1e-9:
            return "filled"
        return "partial" if self.filled > 0 else fallback

    def _instrument(self) -> dict[str, Any]:
        for instrument in self.ctx.get_instruments().get("instruments", []):
            if str(instrument.get("symbol")) == self.spec["symbol"]:
                return instrument
        raise ChaseError(f"unknown symbol {self.spec['symbol']}")

    def _peg_price(self, tick: Decimal, offset: int) -> Decimal:
        ticker = self.ctx.hub.ticker(self.spec["symbol"]) or self.ctx.get_ticker_rest(self.spec["symbol"]) or {}
        bid, ask = ticker.get("bid"), ticker.get("ask")
        if not bid or not ask:
            raise ChaseTransient("no bid/ask available yet")
        if self.spec["side"] == "buy":
            return Decimal(str(bid)) - Decimal(offset) * tick
        return Decimal(str(ask)) + Decimal(offset) * tick

    def _reducible_size(self) -> Decimal:
        try:
            response = self.ctx.client.get("/openpositions", private=True)
            rows = response.get("openPositions") if response.get("result") == "success" else None
            if not isinstance(rows, list) or any(not isinstance(p, dict) or not p.get("symbol") or p.get("error") for p in rows):
                raise ValueError("missing successful openPositions")
            matches = [p for p in rows if p.get("symbol") == self.spec["symbol"]]
            if not matches:
                return Decimal(0)
            if len(matches) != 1:
                raise ValueError("ambiguous position state")
            position = matches[0]
            size = Decimal(str(position.get("size")))
            if not size.is_finite() or size < 0 or position.get("side") not in {"long", "short"}:
                raise ValueError("invalid position size or side")
            closing_side = "sell" if position["side"] == "long" else "buy"
            return size if closing_side == self.spec["side"] else Decimal(0)
        except Exception as exc:
            raise ChaseTransient(f"position read unavailable; no reduce-only placement: {exc}") from exc

    def _place(self, price: Decimal, size: Decimal) -> None:
        if self.spec.get("reduceOnly"):
            size = min(size, self._reducible_size())
            if size <= 0:
                self.stop_reason = "no_reducible_position"
                self._finish(self._terminal_status("cancelled"), "no remaining position to reduce; no order placed")
                return
        cli_id = f"ch-{self.id}-{self.pegs}-{uuid.uuid4().hex[:6]}"
        self._active = {
            "cliOrdId": cli_id,
            "orderId": None,
            "price": price,
            "size": float(size),
            "seenFilled": 0.0,
            "placedAt": time.monotonic(),
        }
        params = {
            "orderType": "post",
            "symbol": self.spec["symbol"],
            "side": self.spec["side"],
            "size": float(size),
            "limitPrice": float(price),
            "cliOrdId": cli_id,
            **({"reduceOnly": True} if self.spec.get("reduceOnly") else {}),
        }
        state = "REPLACING" if self.pegs > 1 else "PLACING"
        self._audit("placement_intent", params=params)
        self._transition(state, f"placing peg {self.pegs}: {self.spec['side']} {size} @ {price} ({cli_id})")
        try:
            response = self.ctx.client.post("/sendorder", params=params, private=True)
        except Exception as exc:
            raise ChaseUnknown(f"placement outcome unknown for {cli_id}: {exc}") from exc
        parsed = parse_operation(response, "sendStatus", "placed")
        if parsed["outcome"] == "rejected":
            self._active = None
            raise ChaseRejected(parsed.get("error") or "placement rejected")
        if parsed["outcome"] != "confirmed":
            raise ChaseUnknown(parsed.get("error") or f"placement outcome unknown for {cli_id}")
        detail = parsed["detail"]
        status = str(parsed["nestedStatus"] or "")
        order_id = parsed.get("exchangeId")
        if not order_id:
            raise ChaseUnknown(f"placement for {cli_id} reported placed without an exchange order ID")
        self._active["orderId"] = str(order_id)
        self._audit("placement_result", cliOrdId=cli_id, orderId=str(order_id), nestedStatus=status)
        self._transition("RESTING", f"peg {self.pegs} resting: {size} @ {price}")

    def _open_orders(self) -> list[dict[str, Any]]:
        try:
            response = self.ctx.client.get("/openorders", private=True)
        except Exception as exc:
            raise ChaseTransient(f"open-orders read failed: {exc}") from exc
        orders = response.get("openOrders") if isinstance(response, dict) and response.get("result") == "success" else None
        if not isinstance(orders, list) or any(not isinstance(o, dict) or o.get("error") for o in orders):
            raise ChaseTransient("open-orders response missing successful openOrders")
        return orders

    def _fill_size(self, order_id: str) -> float:
        try:
            response = self.ctx.client.get("/fills", private=True)
        except Exception as exc:
            raise ChaseTransient(f"fills read failed: {exc}") from exc
        fills = response.get("fills") if isinstance(response, dict) and response.get("result") == "success" else None
        if not isinstance(fills, list):
            raise ChaseTransient("fills response missing successful fills")
        matched = [
            fill for fill in fills
            if isinstance(fill, dict) and str(fill.get("order_id") or fill.get("orderId") or "") == order_id
        ]
        size = sum(float(fill.get("size") or 0) for fill in matched)
        self._audit(
            "fills_observed", orderId=order_id, count=len(matched), size=size,
            fillIds=[str(fill.get("fill_id") or fill.get("fillId") or "") for fill in matched[:20]],
        )
        return size

    def _reconciled_size(self, active: dict[str, Any], status: str, filled: float, order_id: str) -> float:
        # Keep authoritative progress even if the supplementary fills lookup fails.
        active["seenFilled"] = max(float(active.get("seenFilled") or 0), filled)
        self.filled = self._base_filled + active["seenFilled"]
        if status == "CANCELLED":
            return filled  # exact terminal status includes the final cumulative quantity
        if status == "FULLY_EXECUTED" and abs(filled - active["size"]) <= max(1e-12, active["size"] * 1e-9):
            return active["size"]
        return max(active["seenFilled"], self._fill_size(order_id))

    def _order_status(self, active: dict[str, Any]) -> tuple[str, float, str]:
        order_id = str(active.get("orderId") or "")
        cli_id = str(active.get("cliOrdId") or "")
        params = {"cliOrdIds": cli_id} if cli_id else {"orderIds": order_id}
        try:
            response = self.ctx.client.post(
                "/orders/status", params=params, private=True,
            )
        except Exception as exc:
            raise ChaseTransient(f"order-status read failed: {exc}") from exc
        rows = response.get("orders") if isinstance(response, dict) and response.get("result") == "success" else None
        if not isinstance(rows, list):
            raise ChaseTransient("order-status response missing orders")
        for row in rows:
            order = row.get("order") if isinstance(row, dict) else None
            row_order_id = str(order.get("orderId") or order.get("order_id") or "") if isinstance(order, dict) else ""
            row_cli_id = str(order.get("cliOrdId") or "") if isinstance(order, dict) else ""
            if not isinstance(order, dict) or not (row_order_id == order_id if order_id else row_cli_id == cli_id):
                continue
            if cli_id and row_cli_id and row_cli_id != cli_id:
                raise ChaseUnknown("order status identity mismatch")
            status = str(row.get("status") or "").upper()
            filled = self._checked_filled(active, order.get("filled"))
            self._audit("order_status", orderId=row_order_id, cliOrdId=row_cli_id, status=status, filled=filled)
            return status, filled, row_order_id
        if order_id:
            return self._cancelled_history_status(active)
        raise ChaseTransient(f"order status unavailable for {cli_id or order_id}")

    @staticmethod
    def _checked_filled(active: dict[str, Any], value: Any) -> float:
        try:
            filled = Decimal(str(value))
            size = Decimal(str(active["size"]))
            seen = Decimal(str(active.get("seenFilled") or 0))
            if not filled.is_finite() or not seen <= filled <= size:
                raise ValueError("outside observed fills and placed quantity")
            return float(filled)
        except Exception as exc:
            raise ChaseUnknown(f"invalid or contradictory filled quantity: {value}") from exc

    def _cancelled_history_status(self, active: dict[str, Any]) -> tuple[str, float, str]:
        from account_log import _get
        from kraken_client import build_query
        params = {"since": int((self.started - 60) * 1000), "tradeable": self.spec["symbol"],
                  "sort": "desc", "count": 100}
        try:
            for _ in range(5):
                response = _get(self.ctx.client, "/api/history/v3/orders", build_query(params))
                if not isinstance(response.get("elements"), list):
                    raise ChaseTransient("order history is unavailable")
                for element in response["elements"]:
                    cancelled = (element.get("event") or {}).get("OrderCancelled") or {}
                    order = cancelled.get("order") or {}
                    if order.get("uid") != active["orderId"]:
                        continue
                    if (order.get("clientId") != active["cliOrdId"]
                            or order.get("tradeable") != self.spec["symbol"]
                            or str(order.get("direction")).lower() != self.spec["side"]
                            or Decimal(str(order.get("quantity"))) != Decimal(str(active["size"]))):
                        raise ChaseUnknown("cancelled order history identity/quantity mismatch")
                    filled = self._checked_filled(active, order.get("filled"))
                    active["cancelReason"] = cancelled.get("reason")
                    self._audit("historical_cancellation", orderId=active["orderId"],
                                cliOrdId=active["cliOrdId"], filled=filled, reason=cancelled.get("reason"),
                                eventId=element.get("uid"))
                    return "CANCELLED", float(filled), active["orderId"]
                token = response.get("continuationToken")
                if not token:
                    break
                params["continuation_token"] = token
        except ChaseError:
            raise
        except Exception as exc:
            raise ChaseTransient(f"order history read failed: {exc}") from exc
        raise ChaseTransient("exact cancelled order not found within bounded history lookup")

    @staticmethod
    def _matches(order: dict[str, Any], active: dict[str, Any]) -> bool:
        order_id = str(order.get("order_id") or order.get("orderId") or "")
        return bool(
            (active.get("orderId") and order_id == active["orderId"])
            or (active.get("cliOrdId") and str(order.get("cliOrdId") or "") == active["cliOrdId"])
        )

    def _reconcile_resting(self) -> bool:
        active = self._active
        if not active:
            return False
        self._transition("RECONCILING", f"reconciling {active['cliOrdId']}")
        orders = self._open_orders()
        mine = next((order for order in orders if self._matches(order, active)), None)
        if mine is not None:
            observed = float(mine.get("filledSize") or 0)
            active["seenFilled"] = max(float(active.get("seenFilled") or 0), observed)
            self.filled = self._base_filled + active["seenFilled"]
            remaining = mine.get("unfilledSize")
            if ((remaining is not None and float(remaining) <= max(1e-12, active["size"] * 1e-9))
                    or active["seenFilled"] >= active["size"] - max(1e-12, active["size"] * 1e-9)):
                self._base_filled += active["size"]
                self._active = None
                self.filled = self._base_filled
                return True
            self._transition("RESTING", f"resting with {active['seenFilled']}/{active['size']} filled")
            return False

        grace = float(self.spec.get("visibilityGraceSec", 2.0))
        if time.monotonic() - float(active.get("placedAt") or 0) < grace:
            raise ChaseTransient("placed order is not visible yet")
        order_id = str(active.get("orderId") or "")
        if not order_id:
            raise ChaseUnknown(f"{active.get('cliOrdId')} absent with no exchange order ID")
        try:
            status, status_filled, status_order_id = self._order_status(active)
            order_id = status_order_id or order_id
            active["orderId"] = order_id
            fills_filled = self._reconciled_size(active, status, status_filled, order_id)
        except ChaseTransient as exc:
            raise ChaseTransient(f"{active['cliOrdId']} absent; reconciliation read will retry: {exc}") from exc
        active["seenFilled"] = max(float(active.get("seenFilled") or 0), status_filled, fills_filled)
        self.filled = self._base_filled + active["seenFilled"]
        if status == "FULLY_EXECUTED" and active["seenFilled"] >= active["size"] - max(1e-12, active["size"] * 1e-9):
            self._base_filled += active["size"]
            self._active = None
            self.filled = self._base_filled
            return True
        if status == "CANCELLED":
            self._base_filled = self.filled
            self._active = None
            self.stop_reason = active.get("cancelReason") or "externally_cancelled"
            self._finish(self._terminal_status("cancelled"), f"exchange cancellation confirmed for {order_id}; no replacement")
            return False
        if status in {"ENTERED_BOOK", "TRIGGER_PLACED"}:
            raise ChaseTransient(f"{active['cliOrdId']} is still {status} but absent from open orders")
        raise ChaseUnknown(
            f"{active['cliOrdId']} is {status or 'unclassified'} with only "
            f"{active['seenFilled']}/{active['size']} confirmed filled"
        )

    def _cancel_active(self) -> None:
        active = self._active
        if not active:
            return
        if active.get("cancelConfirmed"):
            self._retry_cancel_reconciliation()
            return
        self._audit("cancellation_intent", cliOrdId=active["cliOrdId"], orderId=active.get("orderId"))
        self._transition("CANCEL_REQUESTED", f"cancel requested for {active['cliOrdId']}")
        try:
            response = self.ctx.client.post(
                "/cancelorder", params={"cliOrdId": active["cliOrdId"]}, private=True,
            )
        except Exception as exc:
            # A lost reply is not a lost order: look at the order before calling it unknown.
            self._resolve_unclear_cancel(exc)
            return
        try:
            status, detail = _operation_status(response, "cancelStatus")
        except ChaseRejected as exc:
            raise ChaseUnknown(str(exc)) from exc
        if status != "cancelled":
            raise ChaseUnknown(f"cancellation status {status or 'missing'} for {active['cliOrdId']}: {str(detail)[:160]}")
        self._audit("cancellation_result", cliOrdId=active["cliOrdId"], orderId=active.get("orderId"), nestedStatus=status)
        active["cancelConfirmed"] = True
        self._transition("CANCEL_CONFIRMED", f"cancel confirmed for {active['cliOrdId']}")
        self._retry_cancel_reconciliation()

    def _resolve_unclear_cancel(self, error: Exception) -> None:
        """The cancel request failed in transit (e.g. a TLS handshake timeout), so it may or
        may not have landed. Read the order for up to unclearCancelSec: gone means cancelled
        or filled, which the normal reconciliation tells apart; still resting means the cancel
        never arrived, so it is sent again (by client id, which cannot touch another order).
        Only when neither is established in time does the Chase stop as unknown."""
        active = self._active
        cli_id = active["cliOrdId"]
        self._log(f"cancel outcome unclear ({error}); checking the order before deciding")
        deadline = time.monotonic() + float(self.spec.get("unclearCancelSec", 30.0))
        poll = float(self.spec.get("unclearCancelPollSec", 3.0))
        last = f"{type(error).__name__}: {error}"
        while True:
            gone = confirmed = False
            try:
                if not any(self._matches(order, active) for order in self._open_orders()):
                    gone = True
                else:
                    response = self.ctx.client.post("/cancelorder", params={"cliOrdId": cli_id}, private=True)
                    status, _detail = _operation_status(response, "cancelStatus")
                    confirmed = status == "cancelled"
                    last = f"order still resting; re-sent cancel returned {status or 'no status'}"
            except ChaseError as exc:
                # Only reads and the re-sent cancel run in here; an ambiguous cancel reply
                # (e.g. notFound once the order has filled) is settled by the next read.
                last = str(exc)
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
            if gone or confirmed:
                self._audit("cancellation_resolved", cliOrdId=cli_id, orderId=active.get("orderId"),
                            via="order_absent" if gone else "cancel_resent")
                active["cancelConfirmed"] = True
                self._transition("CANCEL_CONFIRMED", f"{cli_id} {'is off the book' if gone else 'cancel confirmed on retry'}")
                self._retry_cancel_reconciliation()
                return
            if time.monotonic() >= deadline:
                raise ChaseUnknown(f"cancellation outcome unknown for {cli_id}: {last}") from error
            self._log(f"{last}; checking again")
            time.sleep(poll)

    def try_resolve(self) -> bool:
        """Settle a Chase that stopped as unknown, from outside its thread. Nothing is placed:
        an order no longer on the book is reconciled as cancelled or filled; one still resting
        stays unknown for the user. Returns True when the Chase reached a final state."""
        active = self._active
        if self.status != "unknown" or self.is_alive() or not active:
            return False
        try:
            if any(self._matches(order, active) for order in self._open_orders()):
                self.unknown_reason = f"{active['cliOrdId']} is still resting on the book; cancel or keep it on Kraken"
                return False
            active["cancelConfirmed"] = True
            self._reconcile_cancelled()
        except ChaseError as exc:
            self._audit("background_resolve_failed", error=str(exc))
            active.pop("cancelConfirmed", None)
            return False
        self.status = "running"  # _finish records the final status from here
        self._finish(self._terminal_status(self.stop_reason or "cancelled"),
                     "resolved in the background: the order is off the book and reconciled")
        self._publish()
        return True

    def _retry_cancel_reconciliation(self) -> None:
        for attempt in range(3):
            try:
                self._reconcile_cancelled()
                return
            except ChaseTransient as exc:
                if attempt == 2:
                    raise ChaseUnknown(f"cancel confirmed, but reconciliation reads failed after 3 attempts: {exc}") from exc
                self._log(f"cancel confirmed; retrying reconciliation reads only: {exc}")
                time.sleep(1.0)

    def _reconcile_cancelled(self) -> None:
        active = self._active
        if not active or not active.get("cancelConfirmed"):
            raise ChaseUnknown("cancellation is not confirmed")
        self._transition("RECONCILING", f"reconciling cancelled {active['cliOrdId']}")
        try:
            if any(self._matches(order, active) for order in self._open_orders()):
                raise ChaseUnknown(f"{active['cliOrdId']} still appears open after confirmed cancellation")
            order_id = str(active.get("orderId") or "")
            if not order_id:
                raise ChaseUnknown(f"cancelled {active['cliOrdId']} has no exchange order ID for fill reconciliation")
            order_state, status_filled, status_order_id = self._order_status(active)
            order_id = status_order_id or order_id
            active["orderId"] = order_id
            if order_state not in {"CANCELLED", "FULLY_EXECUTED"}:
                raise ChaseUnknown(f"cancelled {active['cliOrdId']} has order state {order_state or 'missing'}")
            fills_filled = self._reconciled_size(active, order_state, status_filled, order_id)
            reconciled = max(float(active.get("seenFilled") or 0), status_filled, fills_filled)
            if order_state == "FULLY_EXECUTED" and reconciled < active["size"] - max(1e-12, active["size"] * 1e-9):
                raise ChaseUnknown("full-execution status has incomplete filled quantity")
            self._audit(
                "cancellation_reconciled", orderId=order_id, orderStatus=order_state,
                statusFilled=status_filled, fillsObserved=fills_filled, reconciledFilled=reconciled,
            )
        except ChaseTransient:
            raise
        self._base_filled += min(active["size"], reconciled)
        self.filled = self._base_filled
        self._active = None
        self._transition("RECONCILED", f"cancel reconciled at {self.filled}/{self.spec['size']} filled")

    def _stop_after_cancel(self, reason: str) -> None:
        if self._active:
            self._cancel_active()
        self.stop_reason = reason
        self._finish(self._terminal_status(reason), f"{reason}; resting order cancelled and reconciled")

    def _wait(self, seconds: float) -> None:
        self._abort.wait(max(0.0, seconds))

    def _run(self) -> None:
        symbol = str(self.spec.get("symbol") or "")
        side = str(self.spec.get("side") or "")
        if not symbol.startswith("PF_"):
            raise ChaseError("Chase supports PF_ contracts only")
        if side not in {"buy", "sell"}:
            raise ChaseError("side must be buy or sell")
        instrument = self._instrument()
        tick = Decimal(str(instrument.get("tickSize") or "0.00000001"))
        precision = int(instrument.get("contractValueTradePrecision") or 0)
        size_total = float(_round_size(Decimal(str(self.spec.get("size") or 0)), precision))
        if size_total <= 0:
            raise ChaseError("size is below the instrument contract lot")
        self.spec["size"] = size_total
        timeout = float(self.spec.get("timeoutSec") or 300)
        repeg_sec = max(0.05, float(self.spec.get("repegSec") or 5))
        max_pegs = int(self.spec.get("maxRepegs") or 120)
        if timeout <= 0 or max_pegs <= 0:
            raise ChaseError("timeoutSec and maxRepegs must be positive")
        deadline = time.monotonic() + timeout
        offset = max(0, int(self.spec.get("offsetTicks") or 0))
        self._log(f"chasing {side} {size_total} {self.spec['symbol']} post-only"
                  f"{' reduce-only' if self.spec.get('reduceOnly') else ''}")

        while self.status == "running":
            if self._abort.is_set():
                self._stop_after_cancel("aborted")
                break
            if time.monotonic() > deadline:
                self._stop_after_cancel("timeout")
                break
            if self.pegs >= max_pegs:
                self._stop_after_cancel("max_repegs")
                break

            if self._active:
                try:
                    if self._reconcile_resting():
                        self._finish(self._terminal_status("cancelled"), "authoritative fills account for the full order")
                        break
                    if self.status != "running":
                        break
                except ChaseTransient as exc:
                    self._log(f"{exc}; leaving current order untouched")
                    self._wait(repeg_sec)
                    continue

            try:
                price = _round_tick(self._peg_price(tick, offset), tick, "down" if side == "buy" else "up")
            except ChaseTransient as exc:
                self._log(str(exc))
                self._wait(repeg_sec)
                continue
            if price <= 0:
                self._log("peg went non-positive")
                self._wait(repeg_sec)
                continue

            current_price = self._active.get("price") if self._active else None
            if self._active is None or price != current_price:
                if self._active:
                    self._cancel_active()
                    if self._base_filled >= size_total - max(1e-12, size_total * 1e-9):
                        self._finish("filled", "full size reconciled during cancellation")
                        break
                remaining = _round_size(Decimal(str(size_total - self._base_filled)), precision)
                if remaining <= 0:
                    self.filled = size_total
                    self._finish("filled", "full size reconciled across pegs")
                    break
                self.pegs += 1
                try:
                    self._place(price, remaining)
                except ChaseRejected as exc:
                    self._active = None
                    self._finish("rejected", str(exc))
                    break
                except ChaseUnknown:
                    raise
                except ChaseTransient as exc:
                    self.pegs -= 1
                    self._log(str(exc))
                if self.status != "running":
                    break
            self._wait(repeg_sec)


class ChaseManager:
    def __init__(self, publish: Any, *, worker_factory: Any = None, orphan_prefix: str = "ch-",
                 venue: str = "kraken") -> None:
        self._chases: dict[str, ChaseWorker] = {}
        self._orphans: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._publish = publish
        # One manager per venue: the worker class and client-id prefix are venue specific.
        self._worker_factory = worker_factory or ChaseWorker
        self._orphan_prefix = orphan_prefix
        self._venue = venue

    def start(self, spec: dict[str, Any], ctx: Any) -> dict[str, Any]:
        worker = self._worker_factory(spec, ctx, self._publish)
        with self._lock:
            if len(self._chases) > 40:
                for key in sorted(self._chases, key=lambda item: self._chases[item].started)[:-20]:
                    if self._chases[key].status != "running":
                        del self._chases[key]
            self._chases[worker.id] = worker
        worker.start()
        return worker.snapshot()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            workers = sorted(self._chases.values(), key=lambda worker: worker.started, reverse=True)
            orphans = list(self._orphans.values())
        return [worker.snapshot() for worker in workers[:20]] + orphans

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            workers = [worker for worker in self._chases.values() if worker.status in {"running", "unknown"}]
            orphans = list(self._orphans.values())
        return [worker.snapshot() for worker in workers] + orphans

    def abort(self, chase_id: str) -> dict[str, Any]:
        with self._lock:
            worker = self._chases.get(chase_id)
            orphan = next((item for item in self._orphans.values() if item["id"] == chase_id), None)
        if orphan:
            return {"error": "orphan Chase orders require an explicit exact-order cancellation"}
        if not worker:
            return {"error": f"unknown chase {chase_id}"}
        if worker.status != "running" or not worker.is_alive():
            return {"error": f"chase {chase_id} is {worker.status}; reconcile or cancel by exact order ID"}
        worker.abort()
        return {"ok": True, "chase": worker.snapshot()}

    def resolve_unknown(self) -> list[str]:
        """One background pass over Chases that stopped as unknown; see ChaseWorker.try_resolve."""
        with self._lock:
            workers = [w for w in self._chases.values() if w.status == "unknown" and not w.is_alive()]
        resolved = []
        for worker in workers:
            try:
                if worker.try_resolve():
                    resolved.append(worker.id)
            except Exception:
                continue
        return resolved

    def acknowledge(self, chase_id: str) -> dict[str, Any]:
        """The user checked an unknown or orphaned Chase on the exchange. Nothing is sent;
        the item stops blocking, and the acknowledgement outlives a restart."""
        with self._lock:
            worker = self._chases.get(chase_id)
            key = next((k for k, item in self._orphans.items() if item["id"] == chase_id), None)
            orphan = self._orphans.pop(key) if key else None
        if orphan:
            original = chase_id[len("recovery-"):] if chase_id.startswith("recovery-") else chase_id
            for item_id in {chase_id, original}:
                self._publish("chase", {**orphan, "id": item_id, "status": "acknowledged", "state": "ACKNOWLEDGED"})
            return {"ok": True}
        if worker and worker.status == "unknown" and not worker.is_alive():
            worker.status, worker.state = "acknowledged", "ACKNOWLEDGED"
            worker._audit("acknowledged")
            worker._publish()
            return {"ok": True, "chase": worker.snapshot()}
        return {"error": f"chase {chase_id} is not waiting for a manual check"}

    def abort_all(self, wait_timeout: float = 5.0, *, chase_ids: set[str] | None = None) -> dict[str, Any]:
        with self._lock:
            running = [worker for worker in self._chases.values() if worker.status == "running" and worker.is_alive()
                       and (chase_ids is None or worker.id in chase_ids)]
        for worker in running:
            worker.abort()
        deadline = time.monotonic() + max(0.0, wait_timeout)
        for worker in running:
            worker.join(max(0.0, deadline - time.monotonic()))
        return {
            "requested": [worker.id for worker in running],
            "completed": [worker.snapshot() for worker in running if not worker.is_alive()],
            "pending": [worker.snapshot() for worker in running if worker.is_alive()],
        }

    def detect_orphans(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found = []
        with self._lock:
            for order in orders:
                cli_id = str(order.get("cliOrdId") or "")
                if not cli_id.lower().startswith(self._orphan_prefix):
                    continue
                order_id = str(order.get("order_id") or order.get("orderId") or "")
                key = order_id or cli_id
                if key in self._orphans:
                    continue
                filled = float(order.get("filledSize") or 0)
                remaining = float(order.get("unfilledSize") or order.get("size") or 0)
                item = {
                    "id": f"orphan-{cli_id}",
                    "exchange": self._venue,
                    "symbol": order.get("symbol"),
                    "side": order.get("side"),
                    "size": filled + remaining,
                    "status": "orphaned",
                    "state": "UNKNOWN",
                    "pegs": None,
                    "filled": filled,
                    "activeCliOrdId": cli_id,
                    "activeOrderId": order_id or None,
                    "unknownReason": "exchange order exists without a live Chase worker",
                    "events": ["Detected on server startup; no automatic cancellation while DISARMED"],
                    "started": 0,
                }
                self._orphans[key] = item
                found.append(item)
        for item in found:
            self._publish("chase", item)
        return found

    def recover(self, snapshots: list[dict[str, Any]], orders: list[dict[str, Any]], ctx: Any = None) -> list[dict[str, Any]]:
        found = self.detect_orphans(orders)
        resolved_ids = set()
        if ctx is not None:
            for snapshot in snapshots:
                if snapshot.get("status") not in {"running", "unknown"}:
                    continue
                cli_id = snapshot.get("activeCliOrdId")
                order_id = snapshot.get("activeOrderId")
                spec = snapshot.get("spec") or {}
                # Only recover a whole-size peg with its persisted placement identity.
                # Partial-size/missing-history cases remain blocked for manual reconciliation.
                placement = next((a.get("params", {}) for a in reversed(snapshot.get("audit", []))
                                  if a.get("event") == "placement_intent" and a.get("params", {}).get("cliOrdId") == cli_id), {})
                if not cli_id or not order_id or not spec.get("size") or placement.get("size") != spec["size"]:
                    continue
                if any(str(order.get("cliOrdId") or "") == cli_id
                       or str(order.get("order_id") or order.get("orderId") or "") == order_id for order in orders):
                    continue
                worker = ChaseWorker(spec.copy(), ctx, lambda *_: None)
                worker.id = snapshot["id"]
                worker.started = snapshot.get("started", worker.started)
                worker.pegs = snapshot.get("pegs", 0)
                worker.audit = list(snapshot.get("audit", []))
                worker._active = {"cliOrdId": cli_id, "orderId": order_id, "size": float(spec["size"]),
                                  "seenFilled": snapshot.get("filled") or 0, "placedAt": 0}
                confirmed_audit = next((a for a in reversed(worker.audit)
                                        if a.get("event") == "order_status" and a.get("orderId") == order_id
                                        and a.get("cliOrdId") in {None, "", cli_id}
                                        and a.get("status") in {"FULLY_EXECUTED", "CANCELLED"}), None)
                cancellation_confirmed = any(a.get("event") == "cancellation_result"
                                             and a.get("orderId") == order_id and a.get("cliOrdId") == cli_id
                                             and a.get("nestedStatus") == "cancelled" for a in worker.audit)
                recovered_status = "filled"
                if confirmed_audit:
                    # Final exchange execution evidence does not expire when /orders/status does.
                    try:
                        quantity = worker._checked_filled(worker._active, confirmed_audit.get("filled"))
                    except ChaseError:
                        continue
                    if confirmed_audit["status"] == "FULLY_EXECUTED" and quantity != float(spec["size"]):
                        continue
                    worker._base_filled = worker.filled = quantity
                    worker._active = None
                    recovered_status = worker._terminal_status("cancelled")
                    if confirmed_audit["status"] == "CANCELLED":
                        worker.stop_reason = "externally_cancelled"
                    worker._audit("recovery_evidence", source="persisted_order_status", orderId=order_id,
                                  filled=worker.filled, sourceEvent=confirmed_audit)
                else:
                    try:
                        if cancellation_confirmed:
                            worker._active["cancelConfirmed"] = True
                            worker._retry_cancel_reconciliation()
                            recovered_status = worker._terminal_status("cancelled")
                        elif not worker._reconcile_resting():
                            if worker.status not in {"cancelled", "partial", "filled"}:
                                continue
                            recovered_status = worker.status
                    except ChaseError:
                        continue
                worker.publish = self._publish
                worker._finish(recovered_status, "startup reconciliation confirmed terminal order state; no exchange writes")
                worker._publish()
                with self._lock:
                    self._chases[worker.id] = worker
                    for key, item in list(self._orphans.items()):
                        if item.get("activeCliOrdId") == cli_id:
                            del self._orphans[key]
                resolved_ids.add(worker.id)
        open_cli_ids = {str(order.get("cliOrdId") or "") for order in orders}
        recovered = []
        with self._lock:
            for snapshot in snapshots:
                if snapshot.get("status") not in {"running", "unknown"} or snapshot.get("id") in resolved_ids:
                    continue
                cli_id = str(snapshot.get("activeCliOrdId") or "")
                if cli_id and cli_id in open_cli_ids:
                    continue
                chase_id = str(snapshot.get("id") or "unknown")
                if chase_id.startswith(("recovery-", "orphan-")):
                    continue
                key = f"recovery:{chase_id}"
                if key in self._orphans:
                    continue
                item = {
                    **snapshot,
                    "id": f"recovery-{chase_id}",
                    "status": "unknown",
                    "state": "UNKNOWN",
                    "unknownReason": snapshot.get("unknownReason")
                    or "previous Chase stopped without a final state; no open order was found and fills require manual reconciliation",
                    "events": ["Recovered from SQLite on server startup; Kraken was not mutated"],
                }
                self._orphans[key] = item
                recovered.append(item)
        for item in recovered:
            self._publish("chase", item)
        return found + recovered

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for worker in self._chases.values() if worker.status == "running" and worker.is_alive())
