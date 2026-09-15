"""Recover prepared batches with at most one upstream status read per call.

This establishes submission identity, not current lifecycle or replacement permission.
"""
from datetime import datetime, timezone
from decimal import Decimal
import re

from hyperliquid_client import HyperliquidError
from hyperliquid_fills import decimal_text

KNOWN = {"observed", "rejected"}


def prepared_orders(action):
    orders = action.get("orders") if isinstance(action, dict) and action.get("type") == "order" else None
    if not isinstance(orders, list) or not 2 <= len(orders) <= 20:
        raise HyperliquidError("Recovery requires a prepared batch of 2 to 20 orders")
    cloids, assets = set(), set()
    for order in orders:
        if (not isinstance(order, dict) or not isinstance(order.get("c"), str) or
                not re.fullmatch(r"0x[0-9a-f]{32}", order["c"]) or order["c"] in cloids or
                type(order.get("a")) is not int or order["a"] < 0 or
                type(order.get("b")) is not bool or type(order.get("r")) is not bool or not isinstance(order.get("s"), str)):
            raise HyperliquidError("Prepared batch has invalid or duplicate identities")
        decimal_text(order.get("s"), positive=True)
        cloids.add(order["c"])
        assets.add(order["a"])
    if len(assets) != 1:
        raise HyperliquidError("Recovery currently supports single-symbol order batches only")
    return orders


def receipt_observations(result, action, orders):
    """Use only complete, action-matched receipts with independently known rows."""
    rows = result.get("rows")
    if (result.get("outcome") not in {"confirmed", "partial", "rejected"} or result.get("type") != "order" or
            result.get("action") != action or not isinstance(rows, list) or len(rows) != len(orders)):
        return None
    observations, ids = {}, set()
    for order, row in zip(orders, rows):
        if not isinstance(row, dict):
            return None
        state = row.get("state")
        if not isinstance(state, str):
            return None
        expected_keys = {"error": {"state", "error"}, "resting": {"state", "oid"},
                         "filled": {"state", "oid", "totalSize", "averagePrice"}}.get(state)
        if expected_keys is None or set(row) != expected_keys:
            return None
        if state == "error":
            if not isinstance(row.get("error"), str) or not row["error"]:
                return None
        elif state in {"resting", "filled"}:
            oid = row.get("oid")
            if type(oid) is not int or not 0 < oid < 2**64 or oid in ids:
                return None
            ids.add(oid)
            if state == "filled":
                try:
                    filled = Decimal(decimal_text(row.get("totalSize"), positive=True))
                    decimal_text(row.get("averagePrice"), positive=True)
                    if filled > Decimal(order["s"]):
                        return None
                except HyperliquidError:
                    return None
        else:
            return None
        observations[order["c"]] = {"state": "rejected" if state == "error" else "observed",
                                     "source": "original_response", "row": row,
                                     "checkedAt": datetime.now(timezone.utc).isoformat()}
    return observations


def advance(db, backend, request_id, target=None):
    if not backend.account_configured:
        raise HyperliquidError("Hyperliquid account is not configured")
    body = db.venue_write_request(request_id, backend.network, backend.account_address)
    action = db.venue_prepared_order(request_id, backend.network, backend.account_address)
    if not body or not isinstance(body.get("symbol"), str):
        raise HyperliquidError("No scoped prepared batch for this request")
    symbol = body["symbol"].strip().upper()
    if not symbol.startswith("HL_"):
        raise HyperliquidError("Prepared batch is not a native Hyperliquid symbol")
    orders = prepared_orders(action)
    cloids = [order["c"] for order in orders]
    if target is not None and target not in cloids:
        raise HyperliquidError("Client id is not in the prepared batch")
    saved = db.venue_recovery_state(request_id, backend.network, backend.account_address)
    if saved["result"].get("simulated") or saved["result"].get("outcome") == "simulated":
        raise HyperliquidError("Simulated batches do not need exchange reconciliation")
    evidence = saved["evidence"]
    receipts = receipt_observations(saved["result"], action, orders)
    if receipts:
        for cloid, observation in receipts.items():
            evidence = db.save_batch_observation(request_id, cloid, observation)
    seen = evidence.get("targets", {})
    remaining = [order for order in orders if seen.get(order["c"], {}).get("state") not in KNOWN]
    if remaining:
        order = next(order for order in orders if order["c"] == target) if target else min(
            remaining, key=lambda row: seen.get(row["c"], {}).get("checkedAt", ""))
        cloid = order["c"]
        if seen.get(cloid, {}).get("state") not in KNOWN:
            observation = {"state": "unknown", "source": "orderStatus", "checkedAt": datetime.now(timezone.utc).isoformat()}
            try:
                status = backend.order_status(cloid)
                observation["status"] = status
                if status.get("found") and not status.get("uncertain"):
                    oid = str(status.get("order_id", ""))
                    if (str(status.get("cliOrdId", "")).lower() != cloid or status.get("symbol") != symbol or
                            not isinstance(status.get("orderStatus"), str) or not status["orderStatus"] or
                            status.get("side") != ("buy" if order["b"] else "sell") or status.get("reduceOnly") is not order["r"] or
                            not re.fullmatch(r"[0-9]{1,20}", oid) or not 0 < int(oid) < 2**64 or
                            Decimal(decimal_text(status.get("originalSizeExact"), positive=True)) != Decimal(order["s"])):
                        raise HyperliquidError("Batch order identity or prepared quantity mismatch")
                    remaining_size = Decimal(decimal_text(status.get("remainingSizeExact")))
                    if not 0 <= remaining_size <= Decimal(order["s"]):
                        raise HyperliquidError("Invalid remaining quantity")
                    observation["state"] = "observed"
            except (HyperliquidError, ValueError) as exc:
                observation["error"] = str(exc)
            evidence = db.save_batch_observation(request_id, cloid, observation)
    return {**evidence, "cloids": cloids, "batch": True, "canReplace": False,
            "exchange": "hyperliquid", "state": "current" if evidence.get("outcome") == "reconciled" else "partial"}
