"""Persisted, price-only amendments of recorded grids. Never cancel or replace orders."""
import uuid
from decimal import ROUND_FLOOR, ROUND_CEILING

from grid import GridError, number
from exchange_ops import parse_operation


def open_orders(ctx):
    response = ctx.client.get("/openorders", private=True)
    rows = response.get("openOrders") if response.get("result") == "success" else None
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise GridError("Current open orders are unavailable.")
    return {str(r.get("order_id") or r.get("orderId") or ""): r for r in rows}


def list_grids(db, ctx, symbol, side):
    current = open_orders(ctx)
    return [{"gridId": g["gridId"], "settings": g["settings"], "originalOrders": len(g["orders"]),
             "workingOrders": sum(o["orderId"] in current for o in g["orders"]),
             "pendingMove": (db.grid_move(grid_id=g["gridId"]) or {}).get("operationId")}
            for g in db.recorded_grids(symbol, side)]


def validate_order(order, symbol, side):
    if order.get("symbol") != symbol or order.get("side") != side:
        raise GridError("Grid order identity changed. No amendments sent.")
    if order.get("orderType") not in {"lmt", "post", "limit"} or str(order.get("reduceOnly")).lower() == "true":
        raise GridError("Only entry-limit grids can be moved. Protection and exits remain untouched.")


def check_prices(rows, ticker, side):
    opposite = number(ticker.get("ask" if side == "buy" else "bid"), "executable quote")
    if any(number(r["toPrice"], "new price") >= opposite if side == "buy"
           else number(r["toPrice"], "new price") <= opposite for r in rows):
        raise GridError("New grid would cross the spread. Use best_bid for a buy or best_ask for a sell; no orders changed.")


def receipt(move):
    counts = {name: sum(r["status"] == name for r in move["orders"])
              for name in ["moved", "unchanged", "failed", "unknown", "not_working", "pending"]}
    return {**move, "counts": counts,
            "summary": ", ".join(f"{n} {key}" for key, n in counts.items()),
            "note": "Price-only amendments. No cancellations, replacement orders, or replenishment of fills. Resume with operationId."}


def move_grid(db, ctx, args, armed):
    symbol, side = str(args.get("symbol") or "").upper(), args.get("side", "buy")
    if not symbol.startswith("PF_") or side not in {"buy", "sell"}:
        raise GridError("Choose a PF_ symbol and buy/sell side.")
    current = open_orders(ctx)
    grids = db.recorded_grids(symbol, side)
    operation_id = str(args.get("operationId") or "")
    move = db.grid_move(operation_id=operation_id) if operation_id else None
    if operation_id and not move:
        raise GridError("Unknown operationId. No new move started.")
    if move:
        if move["symbol"] != symbol or move["side"] != side or (args.get("gridId") and args["gridId"] != move["gridId"]):
            raise GridError("Operation identity does not match the requested grid.")
        if move["outcome"] == "confirmed":
            return receipt(move)
    else:
        candidates = [g for g in grids if g["gridId"] == args["gridId"]] if args.get("gridId") else [
            g for g in grids if any(o["orderId"] in current for o in g["orders"]) or db.grid_move(grid_id=g["gridId"])]
        if len(candidates) != 1:
            raise GridError("No single working grid identified. Call get_grids and select gridId. Cancelled/filled rungs are never recreated.")
        grid = candidates[0]
        move = db.grid_move(grid_id=grid["gridId"])
        if not move:
            working = [current[o["orderId"]] for o in grid["orders"] if o["orderId"] in current]
            if not working:
                raise GridError("This grid has no working orders. Nothing was recreated.")
            for order in working:
                validate_order(order, symbol, side)
            ticker = ctx.fresh_ticker(symbol)
            anchor = args.get("anchor", "last")
            if anchor not in {"last", "best_bid", "best_ask"}:
                raise GridError("anchor must be last, best_bid, or best_ask.")
            target = number(ticker.get({"last": "last", "best_bid": "bid", "best_ask": "ask"}[anchor]), "anchor price")
            tick = number(ctx.instrument(symbol).get("tickSize"), "tick size")
            top = (max if side == "buy" else min)(number(o["limitPrice"], "working price") for o in working)
            delta = target - top
            rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
            rows = []
            for order in working:
                price = number(order["limitPrice"], "working price")
                shifted = ((price + delta) / tick).to_integral_value(rounding=rounding) * tick
                number(shifted, "shifted price")
                rows.append({"orderId": str(order.get("order_id") or order.get("orderId")),
                             "fromPrice": float(price), "toPrice": float(shifted),
                             "remainingAtPlan": float(number(order.get("unfilledSize"), "remaining size")), "status": "pending"})
            if len({r["toPrice"] for r in rows}) != len(rows):
                raise GridError("Tick rounding produced duplicate grid prices.")
            ids = {r["orderId"] for r in rows}
            targets = {r["toPrice"] for r in rows}
            if any(oid not in ids and o.get("symbol") == symbol and o.get("side") == side
                   and float(o.get("limitPrice") or 0) in targets for oid, o in current.items()):
                raise GridError("A new grid price overlaps an unrelated order.")
            check_prices(rows, ticker, side)
            ctx.require_new_exposure(symbol)
            move = {"type": "move_grid", "operationId": uuid.uuid4().hex, "gridId": grid["gridId"],
                    "symbol": symbol, "side": side, "anchor": anchor, "anchorPrice": float(target),
                    "outcome": "pending", "orders": rows}
            if armed:
                db.save_grid_move(move)
    if not armed:
        return {**receipt(move), "outcome": "simulated", "simulated": True}

    # Save intent before each amendment. After a timeout/restart, inspect that ID before retrying.
    for row in move["orders"]:
        if row["status"] in {"moved", "unchanged", "not_working"}:
            continue
        try:
            current = open_orders(ctx)
            order = current.get(row["orderId"])
            if not order:
                if row["status"] == "unknown":
                    raise GridError("Uncertain amendment is now absent. Reconcile its execution history before resuming.")
                row["status"] = "not_working"
                db.save_grid_move(move)
                continue
            validate_order(order, symbol, side)
            price = float(number(order.get("limitPrice"), "current price"))
            if price == row["toPrice"]:
                row["status"] = "unchanged" if row["fromPrice"] == row["toPrice"] else "moved"
                row.pop("error", None)
                db.save_grid_move(move)
                continue
            if price != row["fromPrice"]:
                raise GridError("Order was edited outside this move. Refusing to overwrite it.")
            if row["status"] == "unknown":
                raise GridError("Previous amendment remains uncertain. Refusing a blind retry while the old price is visible.")
            ctx.require_new_exposure(symbol)
            check_prices([r for r in move["orders"] if r["status"] in {"pending", "failed"}], ctx.fresh_ticker(symbol), side)
            row["status"] = "unknown"
            db.save_grid_move(move)
            # Omitting size is intentional: fills during the move must not replenish quantity.
            response = ctx.client.post("/editorder", params={"orderId": row["orderId"], "limitPrice": row["toPrice"]}, private=True)
            parsed = parse_operation(response, "editStatus", "edited")
            if parsed["outcome"] != "confirmed":
                row["status"] = "failed" if parsed["outcome"] == "rejected" else "unknown"
                raise GridError(parsed.get("error") or "Amendment not confirmed")
            row["status"] = "moved"
            row.pop("error", None)
            db.save_grid_move(move)
        except Exception as exc:
            if row["status"] == "pending":
                row["status"] = "failed"
            row["error"] = str(exc)
            break
    move["outcome"] = "confirmed" if all(r["status"] in {"moved", "unchanged", "not_working"} for r in move["orders"]) else "partial"
    db.save_grid_move(move)
    return receipt(move)
