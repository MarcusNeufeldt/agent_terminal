"""Read-only lifecycle checks. A consistent snapshot is not permission to replace an order."""

from decimal import Decimal, localcontext
from datetime import datetime, timezone

from hyperliquid_client import HyperliquidError
from hyperliquid_fills import decimal_text, order_totals

# Explicit known statuses. New venue statuses remain unresolved until reviewed.
CANCELED = {"canceled", "marginCanceled", "vaultWithdrawalCanceled", "openInterestCapCanceled",
            "selfTradeCanceled", "reduceOnlyCanceled", "siblingFilledCanceled", "delistedCanceled",
            "liquidatedCanceled", "scheduledCancel"}
REJECTED = {"rejected", "tickRejected", "minTradeNtlRejected", "perpMarginRejected", "reduceOnlyRejected",
            "badAloPxRejected"}


def inspect_order(db, backend, request_id):
    if not backend.account_configured:
        raise HyperliquidError("Hyperliquid account is not configured")
    body = db.venue_write_request(request_id, backend.network, backend.account_address)
    if not body or not body.get("cloid"):
        raise HyperliquidError("No recoverable order identity for this account and request")
    status = backend.order_status(body["cloid"])
    result = {"state": "current", "exchange": "hyperliquid", "requestId": request_id,
              "checkedAt": datetime.now(timezone.utc).isoformat(),
              "orderStatus": status.get("orderStatus"), "lifecycle": "unknown", "terminal": None,
              "fillEvidence": "unknown", "automationDecision": "wait", "canReplace": False,
              "historyComplete": False}
    if not status.get("found") or status.get("uncertain"):
        return {**result, "reason": "Order not found. This does not prove non-submission."}
    if (status.get("symbol") != body.get("symbol") or status.get("side") != body.get("side") or
            str(status.get("cliOrdId", "")).lower() != body["cloid"].lower() or
            status.get("reduceOnly") is not bool(body.get("reduceOnly", False))):
        raise HyperliquidError("Order lifecycle identity or intent mismatch")
    totals = order_totals(db, backend, status["order_id"])
    if totals["fillCount"] and (totals["symbol"], totals["side"]) != (body["symbol"], body["side"]):
        raise HyperliquidError("Stored fills disagree with the submitted order")
    original = Decimal(decimal_text(status.get("originalSizeExact"), positive=True))
    remaining = Decimal(decimal_text(status.get("remainingSizeExact")))
    requested = Decimal(decimal_text(body.get("size"), positive=True))
    prepared = db.venue_prepared_order(request_id, backend.network, backend.account_address)
    validated = requested
    if prepared is not None:
        orders = prepared.get("orders")
        if prepared.get("type") != "order" or not isinstance(orders, list) or len(orders) != 1:
            raise HyperliquidError("Invalid prepared order record")
        order = orders[0]
        if (order.get("c") != body["cloid"] or order.get("b") is not (body["side"] == "buy") or
                order.get("r") is not bool(body.get("reduceOnly", False))):
            raise HyperliquidError("Prepared order disagrees with request identity")
        validated = Decimal(decimal_text(order.get("s"), positive=True))
    observed = Decimal(totals["observedFilledSize"])
    result.update(orderId=status["order_id"], statusTime=status.get("statusTime"),
                  reportedOriginalSize=str(original), reportedRemainingSize=str(remaining),
                  requestedSize=str(requested), validatedSize=str(validated), hasPreparedOrder=prepared is not None, fills=totals)
    if original != validated:
        return {**result, "fillEvidence": "conflicting", "automationDecision": "stop",
                "reason": "Exchange original size differs from the stored intent. Investigate normalization or external amendments."}
    if remaining < 0 or remaining > original or observed > original:
        return {**result, "fillEvidence": "conflicting", "reason": "Fill or remaining quantity exceeds the order bounds."}
    kind = status["orderStatus"]
    if kind in CANCELED:
        return {**result, "lifecycle": "canceled", "terminal": True, "fillEvidence": "observed-only",
                "automationDecision": "stop", "reason": "Exchange cancellation is terminal. Never recreate automatically."}
    if kind in REJECTED:
        if observed:
            return {**result, "fillEvidence": "conflicting", "reason": "Rejected order has recorded fills; investigate."}
        return {**result, "lifecycle": "rejected", "terminal": True, "automationDecision": "stop",
                "fillEvidence": "observed-only", "reason": "Exchange reported rejection; no automatic replacement."}
    if kind == "triggered":
        return {**result, "lifecycle": "triggered", "reason": "Trigger fired. Child execution must be reconciled separately."}
    if kind not in {"open", "filled"}:
        return {**result, "reason": "Unrecognized exchange status; no automated action is allowed."}
    with localcontext() as context:
        context.prec = 384
        expected = original if kind == "filled" else original - remaining
    result.update(lifecycle=kind, terminal=kind == "filled", reportedExecutedSize=str(expected))
    if kind == "filled" and remaining != 0:
        return {**result, "fillEvidence": "conflicting", "reason": "Filled status reports remaining quantity."}
    if observed != expected:
        return {**result, "fillEvidence": "missing" if observed < expected else "conflicting",
                "reason": "Observed fill total does not match the exchange snapshot. Refresh/backfill before proceeding."}
    return {**result, "fillEvidence": "matched", "automationDecision": "stop" if kind == "filled" else "monitor",
            "reason": "Exact observed fills match the snapshot. Replacement still requires a separate guarded operation."}
