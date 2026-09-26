"""Hyperliquid post-only Chase.

Hyperliquid has no Chase order type on the exchange: its own web UI runs the loop in
the browser tab. This runs the same loop server side so it survives a reload.

Every re-peg is cancel -> confirmed terminal state -> fills reconciled -> new Alo
order, the same guarantee as the Kraken engine: an order is never replaced while its
fill state is unknown, and any uncertain exchange outcome stops the worker for good.

Timeout policy: an entry cancels whatever is unfilled. A reduce-only exit that times
out closes its remainder with a reduce-only IOC market order, but only while ARMED.
An explicit abort (button, disarm) is always cancel-only.
"""

from __future__ import annotations

import secrets
import time
from decimal import Decimal
from typing import Any

from chase import ChaseError, ChaseRejected, ChaseTransient, ChaseUnknown, ChaseWorker
from hyperliquid_client import HyperliquidError
from hyperliquid_lifecycle import CANCELED, REJECTED
from hyperliquid_trading import format_size, market_action_for, order_action_for

# "chas" in hex, so a chase order is recognisable on the exchange after a restart.
CLOID_PREFIX = "0x63686173"
MAX_ALO_REJECTS = 20
MARKET_FINISH_SLIPPAGE = 0.5
VISIBILITY_GRACE_SEC = 5.0
# While a cancelled order still reads open, the cancel is sent again this often.
RESEND_CANCEL_SEC = 5.0


def chase_cloid() -> str:
    return CLOID_PREFIX + secrets.token_hex(12)


def _alo_crossed(error: Any) -> bool:
    text = str(error or "").lower()
    return "post only" in text or "immediately matched" in text or "badalopx" in text


def parse_spec(body: dict[str, Any]) -> dict[str, Any]:
    symbol = str(body.get("symbol") or "").strip().upper()
    side = str(body.get("side") or "").strip().lower()
    size = body.get("size")
    reduce_only = body.get("reduceOnly", False)
    if not symbol.startswith("HL_") or side not in {"buy", "sell"}:
        raise HyperliquidError("HL_ symbol and side (buy|sell) required")
    if isinstance(size, bool) or type(reduce_only) is not bool or type(body.get("expectedArmed")) is not bool:
        raise HyperliquidError("size, reduceOnly and the expected ARM state are required")
    try:
        size = float(size)
        timeout = float(body.get("timeoutSec") or 300)
        max_pegs = int(body.get("maxRepegs") or 120)
        repeg = max(1.0, float(body.get("repegSec") or 5))
    except (TypeError, ValueError) as exc:
        raise HyperliquidError("invalid Chase size or timing") from exc
    if not size > 0 or not 0 < timeout <= 3600 or not 0 < max_pegs <= 1000:
        raise HyperliquidError("Chase needs a positive size, a timeout up to 3600s and 1-1000 re-pegs")
    return {"exchange": "hyperliquid", "symbol": symbol, "side": side, "size": size,
            "reduceOnly": reduce_only, "timeoutSec": timeout, "maxRepegs": max_pegs, "repegSec": repeg,
            # Exits must complete; a missed entry costs nothing.
            "finishMarket": reduce_only}


def _instrument(backend, symbol: str) -> dict[str, Any]:
    instrument = (backend.markets().get(symbol) or {}).get("instrument")
    if not instrument or instrument.get("tradeable") is False:
        raise HyperliquidError("Unknown or untradeable Hyperliquid symbol")
    return instrument


def _peg(book: dict[str, Any], side: str) -> Decimal:
    # Join the best price on our own side. Stepping one tick inside can cross a
    # one-tick spread, which a post-only order refuses anyway.
    try:
        level = book["orderBook"]["bids" if side == "buy" else "asks"][0][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ChaseTransient("no two-sided book yet") from exc
    price = Decimal(str(level))
    if not price.is_finite() or price <= 0:
        raise ChaseTransient("book price is not usable")
    return price


def preview(spec: dict[str, Any], backend) -> dict[str, Any]:
    """The first post-only order a live Chase would place, for DISARMED review."""
    instrument = _instrument(backend, spec["symbol"])
    price = _peg(backend.quote(spec["symbol"]), spec["side"])
    action = order_action_for(instrument, spec["side"], spec["size"], price, tif="alo",
                              reduce_only=spec["reduceOnly"], cloid=chase_cloid())
    return {"action": action, "spec": spec}


class HyperliquidChaseWorker(ChaseWorker):
    """ctx: backend (HyperliquidBackend), submit(action, placement=bool) -> trader result,
    armed() -> bool, verify_signer() -> None."""

    def snapshot(self) -> dict[str, Any]:
        snap = super().snapshot()
        snap["exchange"] = "hyperliquid"
        snap["spec"].update(exchange="hyperliquid", finishMarket=bool(self.spec.get("finishMarket")))
        return snap

    # ---- exchange reads ----

    def _order_state(self, active: dict[str, Any]) -> tuple[str, Decimal]:
        try:
            status = self.ctx.backend.order_status(active["cloid"])
        except HyperliquidError as exc:
            raise ChaseTransient(f"order status read failed: {exc}") from exc
        if not status.get("found"):
            if time.monotonic() - active["placedAt"] < VISIBILITY_GRACE_SEC:
                raise ChaseTransient("placed order is not visible yet")
            raise ChaseUnknown(f"{active['cloid']} was accepted but the exchange no longer reports it")
        if (str(status.get("cliOrdId") or "").lower() != active["cloid"] or status.get("symbol") != self.spec["symbol"]
                or status.get("side") != self.spec["side"]
                or Decimal(str(status.get("originalSizeExact"))) != active["size"]):
            raise ChaseUnknown(f"order status identity mismatch for {active['cloid']}")
        state = str(status.get("orderStatus") or "")
        original = Decimal(str(status.get("originalSizeExact")))
        remaining = Decimal(str(status.get("remainingSizeExact")))
        if not remaining.is_finite() or not 0 <= remaining <= original:
            raise ChaseUnknown(f"invalid remaining size for {active['cloid']}")
        filled = original if state == "filled" else original - remaining
        if filled < active["seen"]:
            raise ChaseUnknown(f"filled quantity went backwards for {active['cloid']}")
        active["seen"] = filled
        self.filled = float(self._base + filled)
        self._audit("order_status", cloid=active["cloid"], oid=status.get("order_id"), status=state,
                    filled=str(filled))
        return state, filled

    def _reducible(self) -> Decimal:
        try:
            positions = self.ctx.backend.positions(fresh=True)["positions"]
        except (HyperliquidError, KeyError, TypeError) as exc:
            raise ChaseTransient(f"position read unavailable; no reduce-only placement: {exc}") from exc
        matches = [p for p in positions if p.get("symbol") == self.spec["symbol"]]
        if not matches:
            return Decimal(0)
        if len(matches) != 1:
            raise ChaseTransient("ambiguous position state")
        closing = "sell" if matches[0].get("side") == "long" else "buy"
        return Decimal(str(matches[0]["sizeExact"])) if closing == self.spec["side"] else Decimal(0)

    # ---- exchange writes ----

    def _submit(self, action: dict[str, Any], *, placement: bool) -> dict[str, Any]:
        result = self.ctx.submit(action, placement=placement)
        if result.get("outcome") == "unknown" or result.get("uncertain"):
            raise ChaseUnknown(f"{action.get('type')} outcome unknown: {result.get('error') or 'no confirmation'}")
        return result

    def _place(self, price: Decimal, size: Decimal) -> None:
        cloid = chase_cloid()
        action = order_action_for(self._instrument_row, self.spec["side"], size, price, tif="alo",
                                  reduce_only=self.spec["reduceOnly"], cloid=cloid)
        placed_size = Decimal(action["orders"][0]["s"])
        self._active = {"cloid": cloid, "cliOrdId": cloid, "orderId": None, "price": price, "size": placed_size,
                        "seen": Decimal(0), "placedAt": time.monotonic()}
        self._audit("placement_intent", params={"cloid": cloid, "price": str(price), "size": str(placed_size)})
        self._transition("REPLACING" if self.pegs > 1 else "PLACING",
                         f"placing peg {self.pegs}: {self.spec['side']} {placed_size} @ {price} post-only")
        result = self._submit(action, placement=True)
        row = (result.get("rows") or [{}])[0]
        if result.get("outcome") == "rejected":
            self._active = None
            if _alo_crossed(result.get("error")):
                raise ChaseTransient(f"book moved before the post-only order landed; re-pegging ({result.get('error')})")
            raise ChaseRejected(result.get("error") or "placement rejected")
        if row.get("state") == "resting":
            self._active["orderId"] = str(row["oid"])
            self._audit("placement_result", cloid=cloid, oid=str(row["oid"]), state="resting")
            self._transition("RESTING", f"peg {self.pegs} resting: {placed_size} @ {price}")
            return
        if row.get("state") == "filled":
            # A post-only order should never fill on arrival; count it only if the size is exact.
            filled = Decimal(str(row.get("totalSize")))
            if filled != placed_size:
                raise ChaseUnknown(f"post-only order reported a partial fill on arrival ({filled}/{placed_size})")
            self._base += filled
            self.filled = float(self._base)
            self._active = None
            self._audit("placement_result", cloid=cloid, oid=str(row.get("oid")), state="filled")
            return
        raise ChaseUnknown(f"unexpected placement status for {cloid}: {row}")

    def _cancel_active(self) -> None:
        active = self._active
        if not active:
            return
        self._audit("cancellation_intent", cloid=active["cloid"], oid=active.get("orderId"))
        self._transition("CANCEL_REQUESTED", f"cancel requested for {active['cloid']}")
        action = {"type": "cancelByCloid", "cancels": [{"asset": self._instrument_row["assetId"], "cloid": active["cloid"]}]}
        # A refused cancel usually means it already filled or was cancelled, and a lost reply
        # (network failure) may or may not have landed. Either way the order status decides.
        result = self._send_cancel(action, active)
        deadline = time.monotonic() + float(self.spec.get("unclearCancelSec", 30.0))
        resend_at = time.monotonic() + RESEND_CANCEL_SEC
        last = result.get("error") or result.get("outcome") or "no status yet"
        while True:
            try:
                state, filled = self._order_state(active)
            except ChaseTransient as exc:
                state, filled, last = None, None, str(exc)
            if state == "filled" or state in CANCELED or state in REJECTED:
                self._base += filled
                self.filled = float(self._base)
                self._active = None
                self._transition("RECONCILED", f"{active['cloid']} {state}; {self.filled}/{self.spec['size']} filled")
                return
            if state is not None:
                last = f"still {state or 'unclassified'} after cancellation"
            if time.monotonic() >= deadline:
                raise ChaseUnknown(f"{active['cloid']} {last}; cancel outcome could not be settled")
            if state == "open" and time.monotonic() >= resend_at:
                # Still resting: the cancel never arrived. Cancel by cloid cannot touch another order.
                self._log(f"{active['cloid']} still open; sending the cancel again")
                self._send_cancel(action, active)
                resend_at = time.monotonic() + RESEND_CANCEL_SEC
            time.sleep(1.0)

    def _send_cancel(self, action: dict[str, Any], active: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self.ctx.submit(action, placement=False)
        except Exception as exc:
            result = {"outcome": "unknown", "error": f"{type(exc).__name__}: {exc}"}
        self._audit("cancellation_result", cloid=active["cloid"], outcome=result.get("outcome"), error=result.get("error"))
        if result.get("outcome") == "unknown" or result.get("uncertain"):
            self._log(f"cancel outcome unclear ({result.get('error') or 'no confirmation'}); checking the order")
        return result

    def try_resolve(self) -> bool:
        """Settle an unknown Chase from outside its thread: an order that is filled or cancelled
        is reconciled; one still open stays unknown. Nothing is placed or cancelled."""
        active = self._active
        if self.status != "unknown" or self.is_alive() or not active:
            return False
        try:
            state, filled = self._order_state(active)
        except ChaseError as exc:
            self._audit("background_resolve_failed", error=str(exc))
            return False
        if not (state == "filled" or state in CANCELED or state in REJECTED):
            self.unknown_reason = f"{active['cloid']} is still {state or 'unclassified'} on Hyperliquid; cancel or keep it there"
            return False
        self._base += filled
        self.filled = float(self._base)
        self._active = None
        self.status = "running"  # _finish records the final status from here
        self._finish(self._terminal_status(self.stop_reason or "cancelled"),
                     f"resolved in the background: {active['cloid']} {state}")
        self._publish()
        return True

    def _market_finish(self, reason: str) -> None:
        if not self.ctx.armed():
            self._log("DISARMED: remainder left open, no market order")
            return
        remaining = self._size_total - self._base
        try:
            quantity = min(remaining, self._reducible())
        except ChaseTransient as exc:
            self._log(f"no market finish: {exc}")
            return
        if quantity <= 0:
            return
        try:
            self.ctx.verify_signer()
            book = self.ctx.backend.quote(self.spec["symbol"])
            action = market_action_for(self._instrument_row, self.spec["side"], quantity, book, MARKET_FINISH_SLIPPAGE,
                                       cloid=chase_cloid(), reduce_only=True)
        except HyperliquidError as exc:
            self._log(f"no market finish: {exc}")
            return
        order = action["orders"][0]
        self._audit("market_finish_intent", cloid=order["c"], size=order["s"], price=order["p"], reason=reason)
        self._transition("MARKET_FINISH", f"{reason}: closing remaining {order['s']} with a reduce-only market order")
        result = self._submit(action, placement=True)
        row = (result.get("rows") or [{}])[0]
        if result.get("outcome") == "confirmed" and row.get("state") == "filled":
            filled = Decimal(str(row["totalSize"]))
            self._base += filled
            self.filled = float(self._base)
            self._audit("market_finish_result", cloid=order["c"], filled=str(filled), price=row.get("averagePrice"))
            self.stop_reason = f"{reason}_market"
            return
        self._audit("market_finish_result", cloid=order["c"], outcome=result.get("outcome"), error=result.get("error"))
        self._log(f"market finish did not fill: {result.get('error') or result.get('outcome')}")

    # ---- loop ----

    def _stop(self, reason: str, *, market: bool) -> None:
        if self._active:
            self._cancel_active()
        self.stop_reason = reason
        if market and self.spec.get("finishMarket") and self._base < self._size_total:
            self._market_finish(reason)
        self._finish(self._terminal_status(reason), f"{self.stop_reason}; resting order cancelled and reconciled")

    def _check_active(self) -> None:
        active = self._active
        state, filled = self._order_state(active)
        if state == "open":
            return
        if state == "filled":
            self._base += active["size"]
            self.filled = float(self._base)
            self._active = None
            return
        if state in CANCELED or state in REJECTED:
            self._base += filled
            self.filled = float(self._base)
            self._active = None
            self.stop_reason = "externally_cancelled"
            self._finish(self._terminal_status("cancelled"), f"exchange reports {state}; no replacement")
            return
        raise ChaseUnknown(f"{active['cloid']} is {state or 'unclassified'}")

    def _run(self) -> None:
        if not str(self.spec.get("symbol") or "").startswith("HL_") or self.spec.get("side") not in {"buy", "sell"}:
            raise ChaseError("HL_ symbol and side (buy|sell) required")
        try:
            self._instrument_row = _instrument(self.ctx.backend, self.spec["symbol"])
            precision = self._instrument_row["contractValueTradePrecision"]
            self._size_total = Decimal(format_size(self.spec["size"], precision))
        except HyperliquidError as exc:
            raise ChaseError(str(exc)) from exc
        self._base = Decimal(0)
        self.spec["size"] = float(self._size_total)
        timeout = float(self.spec.get("timeoutSec") or 300)
        repeg_sec = max(0.05, float(self.spec.get("repegSec") or 5))
        max_pegs = int(self.spec.get("maxRepegs") or 120)
        deadline = time.monotonic() + timeout
        alo_rejects = 0
        self._log(f"chasing {self.spec['side']} {self._size_total} {self.spec['symbol']} post-only"
                  f"{' reduce-only, market finish on timeout' if self.spec.get('finishMarket') else ''}")

        while self.status == "running":
            if self._abort.is_set():
                self._stop("aborted", market=False)
                break
            if time.monotonic() > deadline:
                self._stop("timeout", market=True)
                break
            if self.pegs >= max_pegs:
                self._stop("max_repegs", market=True)
                break
            try:
                if self._active:
                    self._check_active()
                    if self.status != "running":
                        break
                if self._base >= self._size_total:
                    self._finish("filled", "full size filled")
                    break
                price = _peg(self.ctx.backend.quote(self.spec["symbol"]), self.spec["side"])
                if self._active and price == self._active["price"]:
                    self._wait(repeg_sec)
                    continue
                if self._active:
                    self._cancel_active()
                    if self._base >= self._size_total:
                        self._finish("filled", "full size filled during cancellation")
                        break
                remaining = self._size_total - self._base
                if self.spec.get("reduceOnly"):
                    remaining = min(remaining, self._reducible())
                    if remaining <= 0:
                        self.stop_reason = "no_reducible_position"
                        self._finish(self._terminal_status("cancelled"), "no remaining position to reduce; no order placed")
                        break
                try:
                    remaining = Decimal(format_size(remaining, self._instrument_row["contractValueTradePrecision"]))
                except HyperliquidError:
                    self._finish(self._terminal_status("cancelled"), "remainder is below one contract lot")
                    break
                self.pegs += 1
                self._place(price, remaining)
                alo_rejects = 0
            except ChaseTransient as exc:
                if "post-only" in str(exc):
                    alo_rejects += 1
                    if alo_rejects > MAX_ALO_REJECTS:
                        self._stop("post_only_rejections", market=True)
                        break
                self._log(str(exc))
            except ChaseRejected as exc:
                self._active = None
                self._finish(self._terminal_status("rejected"), str(exc))
                break
            except HyperliquidError as exc:
                self._log(f"{exc}; leaving current order untouched")
            self._wait(repeg_sec)
