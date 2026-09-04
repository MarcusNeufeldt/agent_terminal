"""Post-only limit chase engine.

A chase places a post-only limit order at the best bid (buy) or best ask
(sell) and re-pegs it as the market moves, so the order only ever rests on
the book and fills pay maker fees. Stops on: filled, timeout, max re-pegs,
abort. Partial fills are preserved across re-pegs (remaining size follows).

Runs as one thread per chase inside the terminal server. Status is broadcast
over SSE ("chase" events) and retrievable via the manager.
"""

from __future__ import annotations

import threading
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any


class ChaseError(Exception):
    pass


def _round_tick(value: Decimal, tick: Decimal, direction: str) -> Decimal:
    q = value / tick
    return q.to_integral_value(rounding=ROUND_DOWN if direction == "down" else ROUND_UP) * tick


def _round_size(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)


class ChaseWorker(threading.Thread):
    def __init__(self, spec: dict[str, Any], ctx: Any, publish: Any):
        super().__init__(daemon=True, name=f"chase-{uuid.uuid4().hex[:6]}")
        self.spec = dict(spec)
        self.ctx = ctx
        self.publish = publish
        self.id = uuid.uuid4().hex[:8]
        self.status = "running"
        self.pegs = 0
        self.filled = 0.0
        self.events: list[str] = []
        self.started = time.time()
        self._abort = threading.Event()

    # ---- plumbing ----

    def abort(self) -> None:
        self._abort.set()

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            if self.status == "running":
                self.status = "error"
            self._log(f"error: {type(exc).__name__}: {exc}")
        self.publish("chase", self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "symbol": self.spec.get("symbol"),
            "side": self.spec.get("side"),
            "size": self.spec.get("size"),
            "status": self.status,
            "pegs": self.pegs,
            "filled": self.filled,
            "events": self.events[-8:],
            "started": self.started,
        }

    def _log(self, msg: str) -> None:
        self.events.append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.publish("chase", self.snapshot())

    # ---- engine ----

    def _instrument(self) -> dict[str, Any]:
        for i in self.ctx.get_instruments().get("instruments", []):
            if str(i.get("symbol")) == self.spec["symbol"]:
                return i
        raise ChaseError(f"unknown symbol {self.spec['symbol']}")

    def _peg_price(self, tick: Decimal, offset: int) -> Decimal:
        t = self.ctx.hub.ticker(self.spec["symbol"]) or self.ctx.get_ticker_rest(self.spec["symbol"]) or {}
        bid, ask = t.get("bid"), t.get("ask")
        if not bid or not ask:
            raise ChaseError("no bid/ask available yet")
        if self.spec["side"] == "buy":
            return Decimal(str(bid)) - Decimal(offset) * tick
        return Decimal(str(ask)) + Decimal(offset) * tick

    def _place(self, price: Decimal, size: Decimal) -> str:
        cli = f"ch-{self.id}-{self.pegs}"
        params = {
            "orderType": "post",
            "symbol": self.spec["symbol"],
            "side": self.spec["side"],
            "size": float(size),
            "limitPrice": float(price),
            "cliOrdId": cli,
        }
        resp = self.ctx.client.post("/sendorder", params=params, private=True)
        result = resp.get("result") if isinstance(resp, dict) else None
        if result != "success":
            status = resp.get("sendStatus") if isinstance(resp, dict) else resp
            raise ChaseError(f"place rejected: {str(status)[:160]}")
        return cli

    def _cancel(self, cli: str) -> None:
        try:
            self.ctx.client.post("/cancelorder", params={"cliOrdId": cli}, private=True)
        except Exception:
            pass

    def _open_orders(self) -> list[dict[str, Any]] | None:
        """Fetch open orders; None on transient failure (caller retries the cycle)."""
        try:
            return self.ctx.client.get("/openorders", private=True).get("openOrders", [])
        except Exception as exc:
            self._log(f"openorders poll failed: {str(exc)[:120]} \u2014 retrying")
            return None

    def _run(self) -> None:
        inst = self._instrument()
        tick = Decimal(str(inst.get("tickSize") or "0.00000001"))
        precision = int(inst.get("contractValueTradePrecision") or 0)
        side = str(self.spec.get("side"))
        size_total = float(self.spec.get("size") or 0)
        if side not in {"buy", "sell"}:
            raise ChaseError("side must be buy or sell")
        if size_total <= 0:
            raise ChaseError("size must be positive")
        timeout = float(self.spec.get("timeoutSec") or 300)
        repeg_sec = max(1.0, float(self.spec.get("repegSec") or 5))
        max_pegs = int(self.spec.get("maxRepegs") or 120)
        deadline = time.monotonic() + timeout

        base_filled = 0.0
        current_cli: str | None = None
        current_price: Decimal | None = None
        offset = max(0, int(self.spec.get("offsetTicks") or 0))
        self._log(f"chasing {side} {size_total} {self.spec['symbol']} post-only")

        def total_filled() -> float:
            return base_filled

        while True:
            if self._abort.is_set():
                self.status = "aborted"
                break
            if time.monotonic() > deadline:
                self.status = "timeout"
                break
            if self.pegs >= max_pegs:
                self.status = "max_repegs"
                break

            # fill check on the resting order
            if current_cli:
                orders = self._open_orders()
                if orders is None:
                    time.sleep(repeg_sec)
                    continue
                mine = next((o for o in orders if o.get("cliOrdId") == current_cli), None)
                if mine is None:
                    # vanished without our cancel => fully filled
                    self.filled = size_total
                    self.status = "filled"
                    self._log("order left the book — fully filled")
                    break
                now_filled = base_filled + float(mine.get("filledSize") or 0)
                if now_filled > self.filled + 1e-12:
                    self.filled = now_filled
                    self._log(f"filled {self.filled}/{size_total}")
                remaining = size_total - now_filled
                if remaining <= max(1e-12, size_total * 1e-9):
                    self.filled = size_total
                    self.status = "filled"
                    self._log("fully filled")
                    break

            # peg computation
            try:
                price = _round_tick(self._peg_price(tick, offset), tick, "down" if side == "buy" else "up")
            except ChaseError as exc:
                self._log(str(exc))
                time.sleep(repeg_sec)
                continue
            if price <= 0:
                self._log("peg went non-positive")
                time.sleep(repeg_sec)
                continue

            need_peg = current_cli is None or price != current_price
            if need_peg:
                if current_cli:
                    # lock in partial fill before re-peging
                    orders = self._open_orders()
                    if orders is None:
                        time.sleep(repeg_sec)
                        continue  # keep the current order resting; re-peg next cycle
                    mine = next((o for o in orders if o.get("cliOrdId") == current_cli), None)
                    if mine:
                        base_filled += float(mine.get("filledSize") or 0)
                    self._cancel(current_cli)
                    current_cli = None
                remaining = Decimal(str(size_total)) - Decimal(str(base_filled))
                remaining = _round_size(remaining, precision)
                if remaining <= 0:
                    self.filled = size_total
                    self.status = "filled"
                    self._log("fully filled across pegs")
                    break
                self.pegs += 1
                try:
                    current_cli = self._place(price, remaining)
                    current_price = price
                    self._log(f"peg {self.pegs}: {side} {remaining} @ {price}")
                except ChaseError as exc:
                    current_price = None
                    # most common cause: post-only would cross -> step a tick more passive
                    self._log(f"{exc} — stepping a tick passive")
                    offset += 1
                    self.pegs -= 1  # a rejection is not a peg
            time.sleep(repeg_sec)

        if current_cli and self.status != "filled":
            self._cancel(current_cli)
        if self.status == "running":
            self.status = "filled" if self.filled >= size_total - 1e-9 else self.status
        self._log(f"chase {self.status}: filled {self.filled}/{size_total}")


class ChaseManager:
    def __init__(self, publish: Any) -> None:
        self._chases: dict[str, ChaseWorker] = {}
        self._lock = threading.Lock()
        self._publish = publish

    def start(self, spec: dict[str, Any], ctx: Any) -> dict[str, Any]:
        worker = ChaseWorker(spec, ctx, self._publish)
        with self._lock:
            if len(self._chases) > 40:
                for k in sorted(self._chases, key=lambda k: self._chases[k].started)[:-20]:
                    if self._chases[k].status != "running":
                        del self._chases[k]
            self._chases[worker.id] = worker
        worker.start()
        return worker.snapshot()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            workers = sorted(self._chases.values(), key=lambda w: w.started, reverse=True)
        return [w.snapshot() for w in workers[:20]]

    def abort(self, chase_id: str) -> dict[str, Any]:
        with self._lock:
            w = self._chases.get(chase_id)
        if not w:
            return {"error": f"unknown chase {chase_id}"}
        w.abort()
        return {"ok": True, "chase": w.snapshot()}

    def running_count(self) -> int:
        return sum(1 for w in self._chases.values() if w.status == "running" and w.is_alive())
