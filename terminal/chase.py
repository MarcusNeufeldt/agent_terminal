"""Reconciled post-only limit Chase engine.

Live Chase remains disabled at the HTTP/action boundary until this state machine
has passed a controlled Kraken demo test. The engine never treats an order that
is merely absent from open orders as filled, and never replaces an order before
a confirmed cancellation and fill reconciliation.
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
        if status == "unknown":
            self.unknown_reason = message
        self._audit("final", status=status, filled=self.filled, reason=message)
        self._log(f"chase {status}: {message}; filled {self.filled}/{self.spec.get('size')}")

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

    def _place(self, price: Decimal, size: Decimal) -> None:
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
        if not isinstance(orders, list):
            raise ChaseTransient("open-orders response missing successful openOrders")
        return [order for order in orders if isinstance(order, dict)]

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
            if not isinstance(order, dict) or not ((order_id and row_order_id == order_id) or (cli_id and row_cli_id == cli_id)):
                continue
            status = str(row.get("status") or "").upper()
            filled = float(order.get("filled") or 0)
            self._audit("order_status", orderId=row_order_id, cliOrdId=row_cli_id, status=status, filled=filled)
            return status, filled, row_order_id
        raise ChaseTransient(f"order status unavailable for {cli_id or order_id}")

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
            fills_filled = self._fill_size(order_id)
        except ChaseTransient as exc:
            raise ChaseUnknown(f"{active['cliOrdId']} absent and cannot be reconciled: {exc}") from exc
        active["seenFilled"] = max(float(active.get("seenFilled") or 0), status_filled, fills_filled)
        self.filled = self._base_filled + active["seenFilled"]
        if status == "FULLY_EXECUTED" and active["seenFilled"] >= active["size"] - max(1e-12, active["size"] * 1e-9):
            self._base_filled += active["size"]
            self._active = None
            self.filled = self._base_filled
            return True
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
        self._audit("cancellation_intent", cliOrdId=active["cliOrdId"], orderId=active.get("orderId"))
        self._transition("CANCEL_REQUESTED", f"cancel requested for {active['cliOrdId']}")
        try:
            response = self.ctx.client.post(
                "/cancelorder", params={"cliOrdId": active["cliOrdId"]}, private=True,
            )
        except Exception as exc:
            raise ChaseUnknown(f"cancellation outcome unknown for {active['cliOrdId']}: {exc}") from exc
        try:
            status, detail = _operation_status(response, "cancelStatus")
        except ChaseRejected as exc:
            raise ChaseUnknown(str(exc)) from exc
        if status != "cancelled":
            raise ChaseUnknown(f"cancellation status {status or 'missing'} for {active['cliOrdId']}: {str(detail)[:160]}")
        self._audit("cancellation_result", cliOrdId=active["cliOrdId"], orderId=active.get("orderId"), nestedStatus=status)
        self._transition("CANCEL_CONFIRMED", f"cancel confirmed for {active['cliOrdId']}")
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
            fills_filled = self._fill_size(order_id)
            reconciled = max(float(active.get("seenFilled") or 0), status_filled, fills_filled)
            if order_state == "FULLY_EXECUTED":
                reconciled = active["size"]
            self._audit(
                "cancellation_reconciled", orderId=order_id, orderStatus=order_state,
                statusFilled=status_filled, fillsObserved=fills_filled, reconciledFilled=reconciled,
            )
        except ChaseTransient as exc:
            raise ChaseUnknown(f"cancelled {active['cliOrdId']} but reconciliation failed: {exc}") from exc
        self._base_filled += min(active["size"], reconciled)
        self.filled = self._base_filled
        self._active = None
        self._transition("RECONCILED", f"cancel reconciled at {self.filled}/{self.spec['size']} filled")

    def _stop_after_cancel(self, reason: str) -> None:
        if self._active:
            self._cancel_active()
        self.stop_reason = reason
        size_total = float(self.spec.get("size") or 0)
        status = "partial" if 0 < self._base_filled < size_total else reason
        self._finish(status, f"{reason}; resting order cancelled and reconciled")

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
                        self._finish("filled", "authoritative fills account for the full order")
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
            self._wait(repeg_sec)


class ChaseManager:
    def __init__(self, publish: Any) -> None:
        self._chases: dict[str, ChaseWorker] = {}
        self._orphans: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._publish = publish

    def start(self, spec: dict[str, Any], ctx: Any) -> dict[str, Any]:
        worker = ChaseWorker(spec, ctx, self._publish)
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

    def abort_all(self, wait_timeout: float = 5.0) -> dict[str, Any]:
        with self._lock:
            running = [worker for worker in self._chases.values() if worker.status == "running" and worker.is_alive()]
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
                if not cli_id.startswith("ch-"):
                    continue
                order_id = str(order.get("order_id") or order.get("orderId") or "")
                key = order_id or cli_id
                if key in self._orphans:
                    continue
                filled = float(order.get("filledSize") or 0)
                remaining = float(order.get("unfilledSize") or order.get("size") or 0)
                item = {
                    "id": f"orphan-{cli_id}",
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

    def recover(self, snapshots: list[dict[str, Any]], orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        found = self.detect_orphans(orders)
        open_cli_ids = {str(order.get("cliOrdId") or "") for order in orders}
        recovered = []
        with self._lock:
            for snapshot in snapshots:
                if snapshot.get("status") not in {"running", "unknown"}:
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
