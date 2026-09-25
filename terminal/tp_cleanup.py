"""Cancel leftover orders only after an observed full-position TP executes and the pair is flat."""
from decimal import Decimal
import json

from actions import ActionError, _checked_rows, cancel_one, is_protection_order


def order_id(order):
    return str(order.get("order_id") or order.get("orderId") or "")


class TakeProfitCleanup:
    def __init__(self, db, ctx, chase, refresh, publish):
        self.db, self.ctx, self.chase = db, ctx, chase
        self.refresh, self.publish = refresh, publish
        self.states = {s["symbol"]: s for s in db.tp_cleanup_states()}
        self.notices = {}

    def snapshot(self):
        self.refresh()
        positions = _checked_rows(self.ctx.get_positions(), "positions")
        orders = _checked_rows(self.ctx.get_orders(), "orders")
        return {p["symbol"]: p for p in positions if float(p.get("size") or 0) > 0}, orders

    def save(self, state, notify=False):
        self.states[state["symbol"]] = state
        self.db.save_tp_cleanup(state)
        notice = json.dumps(state, sort_keys=True)
        if notify and self.notices.get(state["symbol"]) != notice:
            self.notices[state["symbol"]] = notice
            self.db.log_event("tp_cleanup", state)
            self.publish("tp_cleanup", state)

    def executed(self, tp):
        response = self.ctx.client.post("/orders/status", params={"orderIds": tp["orderId"]}, private=True)
        if response.get("result") != "success" or not isinstance(response.get("orders"), list):
            raise ActionError("TP execution status unavailable")
        for row in response["orders"]:
            order = row.get("order") or {}
            if order_id(order) == tp["orderId"]:
                return row.get("status") == "FULLY_EXECUTED" and Decimal(str(order.get("filled") or 0)) >= Decimal(str(tp["size"]))
        raise ActionError("Exact TP order is absent from status history; cleanup remains blocked")

    def step(self, armed):
        # Caller holds the shared execution/ARM lock for this entire step.
        positions, orders = self.snapshot()
        for symbol, position in positions.items():
            if not symbol.startswith("PF_"):
                continue
            existing = self.states.get(symbol, {})
            if existing.get("status") == "pending":
                self.save({**existing, "status": "paused", "error": "Position reopened before cleanup finished; no automatic close or further cancellation."}, True)
                continue
            side = "sell" if position["side"] == "long" else "buy"
            tps = [{"orderId": order_id(o), "size": o.get("unfilledSize", o.get("size"))}
                   for o in orders if o.get("symbol") == symbol and o.get("side") == side
                   and is_protection_order(o, "TP")
                   and order_id(o) and float(o.get("unfilledSize", o.get("size")) or 0) >= float(position["size"])]
            if existing.get("status") == "paused" and not any(tp["orderId"] not in {old["orderId"] for old in existing["tps"]} for tp in tps):
                continue
            if tps:
                state = {"symbol": symbol, "status": "watching", "tps": tps}
                if state != existing:
                    self.save(state)
            elif existing.get("status") == "watching":
                # Positions and orders are separate REST reads. Allow one mixed snapshot at a fill.
                missing = existing.get("missingObservations", 0) + 1
                self.save({**existing, "missingObservations": missing,
                           "status": "inactive" if missing >= 2 else "watching"})

        for symbol, state in list(self.states.items()):
            if symbol in positions or state.get("status") not in {"watching", "pending"}:
                continue
            if state["status"] == "watching":
                try:
                    confirmed_tp = any(self.executed(tp) for tp in state["tps"])
                except Exception as exc:
                    error = str(exc)
                    if state.get("error") != error:
                        self.save({**state, "error": error}, True)
                    continue
                if not confirmed_tp:
                    self.save({**state, "status": "inactive"})
                    continue
                targets = [order_id(o) for o in orders if o.get("symbol") == symbol and order_id(o)]
                state.pop("error", None)
                state = {**state, "status": "pending", "targets": targets, "results": {},
                         "chaseIds": [c["id"] for c in self.chase.active() if c.get("symbol") == symbol]}
                self.save(state, True)
            if not armed:
                continue
            stopped = self.chase.abort_all(chase_ids=set(state["chaseIds"]))
            if stopped.get("pending"):
                self.save({**state, "error": "Waiting for this pair's Chase workers to stop."}, True)
                continue
            positions_now, current = self.snapshot()
            if symbol in positions_now:
                self.save({**state, "status": "paused", "error": "Position reopened during cleanup; remaining orders left untouched."}, True)
                continue
            # A captured Chase may have placed one last peg before stopping.
            for order in current:
                if order.get("symbol") == symbol and any(str(order.get("cliOrdId") or "").startswith(f"ch-{cid}-") for cid in state["chaseIds"]):
                    if order_id(order) and order_id(order) not in state["targets"]:
                        state["targets"].append(order_id(order))
            self.save(state)
            for target in state["targets"]:
                if state["results"].get(target, {}).get("outcome") in {"confirmed", "not_working"}:
                    continue
                positions_now, current = self.snapshot()
                if symbol in positions_now:
                    state.update(status="paused", error="Position reopened during cleanup; remaining orders left untouched.")
                    break
                match = next((o for o in current if order_id(o) == target), None)
                if match is None:
                    state["results"][target] = {"outcome": "not_working"}
                elif match.get("symbol") != symbol:
                    raise ActionError("Cleanup order identity mismatch")
                else:
                    state["results"][target] = {"outcome": "unknown"}
                    self.save(state)  # persist cancellation intent before the exchange call
                    result = cancel_one(self.ctx, {"order_id": target})
                    state["results"][target] = {k: result.get(k) for k in ["outcome", "error"]}
                    self.db.log_action("tp_cleanup", True, [{"type": "cancel", "symbol": symbol, "orderId": target}], [result])
                self.save(state)
                if state["results"][target]["outcome"] not in {"confirmed", "not_working"}:
                    break
            if state["status"] != "paused" and all(state["results"].get(t, {}).get("outcome") in {"confirmed", "not_working"} for t in state["targets"]):
                _, final_orders = self.snapshot()
                remaining = {order_id(o) for o in final_orders} & set(state["targets"])
                if remaining:
                    for target in remaining:
                        state["results"][target] = {"outcome": "unknown", "error": "Order still appears open after cancellation."}
                else:
                    state["status"] = "completed"
                    state.pop("error", None)
            state["cancelledCount"] = sum(r.get("outcome") == "confirmed" for r in state["results"].values())
            self.save(state, True)
