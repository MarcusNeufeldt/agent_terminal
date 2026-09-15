"""Server-owned submission recovery. Never signs, submits or retries exchange actions."""

from datetime import datetime, timezone
import re
import time

from hyperliquid_client import HyperliquidError
import hyperliquid_batch_recovery
import hyperliquid_chart


def unresolved(db, backend):
    if not backend.account_configured:
        raise HyperliquidError("Hyperliquid account is not configured")
    result = db.venue_unresolved(backend.network, backend.account_address)
    for item in result["items"]:
        action = db.venue_prepared_order(item["requestId"], backend.network, backend.account_address)
        if item.get("endpoint") == "/api/leverage":
            item["kind"] = "leverage"
        if item.get("endpoint") == "/api/chart-order":
            item["kind"] = "chart"
        is_batch = isinstance(action, dict) and isinstance(action.get("orders"), list) and len(action["orders"]) > 1
        if is_batch or item.get("endpoint") == "/api/grid":
            try:
                item.update(batch=True, cloids=[order["c"] for order in hyperliquid_batch_recovery.prepared_orders(action)])
            except HyperliquidError as exc:
                item["recoveryError"] = str(exc)
    return {**result, "state": "current", "exchange": "hyperliquid"}


def cancel_scope(body):
    if isinstance(body, dict) and "targets" in body:
        targets = body["targets"]
        if (not isinstance(targets, list) or not 1 <= len(targets) <= 100 or
                any(body.get(key) is not None for key in ("symbol", "asset", "orderId", "orderIds", "cliOrdId"))):
            raise HyperliquidError("Account cancellation requires 1 to 100 frozen targets without mixed selectors")
        scope = {}
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"symbol", "orderId"} or not isinstance(target["orderId"], str):
                raise HyperliquidError("Each target requires one symbol and an exact decimal-string order id")
            identifier = cancel_targets(target)[0]
            symbol = target["symbol"].strip().upper()
            if not symbol.startswith("HL_") or identifier in scope:
                raise HyperliquidError("Duplicate or non-Hyperliquid cancellation target")
            scope[identifier] = symbol
        return scope
    return {target: body["symbol"].strip().upper() for target in cancel_targets(body)}


def cancel_targets(body):
    if isinstance(body, dict) and "targets" in body:
        return list(cancel_scope(body))
    if not isinstance(body, dict) or not isinstance(body.get("symbol"), str) or not body["symbol"].strip():
        raise HyperliquidError("Legacy cancellation lacks a recoverable symbol")
    if "orderIds" in body and any(body.get(key) is not None for key in ("orderId", "cliOrdId", "asset")):
        raise HyperliquidError("Ambiguous saved cancellation scope")
    if body.get("cliOrdId"):
        value = body["cliOrdId"]
        if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{32}", value):
            raise HyperliquidError("Invalid saved cancellation client id")
        return [value.lower()]
    values = body.get("orderIds", [body.get("orderId")])
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise HyperliquidError("Invalid saved cancellation scope")
    targets = []
    for value in values:
        text = str(value)
        if not re.fullmatch(r"[0-9]{1,20}", text) or not 0 < int(text) < 2**64:
            raise HyperliquidError("Invalid saved cancellation order id")
        targets.append(str(int(text)))
    if len(set(targets)) != len(targets):
        raise HyperliquidError("Duplicate saved cancellation targets")
    return targets


def cancellations(db, backend, request_id=None):
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", request_id)):
        raise HyperliquidError("Valid cancellation request id required")
    if not backend.account_configured:
        raise HyperliquidError("Hyperliquid account is not configured")
    result = db.venue_cancellations(backend.network, backend.account_address, request_id)
    for item in result["items"]:
        try:
            item["symbols"] = cancel_scope(item["body"])
            item["targets"] = list(item["symbols"])
        except HyperliquidError as exc:
            item.update(targets=[], recoveryError=str(exc))
    return {**result, "state": "current", "exchange": "hyperliquid"}


def reconcile_cancel(db, backend, request_id, target):
    if not isinstance(request_id, str) or not request_id:
        raise HyperliquidError("Cancellation request id is required")
    items = cancellations(db, backend, request_id)["items"]
    if len(items) != 1 or not isinstance(target, str) or target not in items[0]["targets"]:
        raise HyperliquidError("Target is not in this account's saved cancellation scope")
    item = items[0]
    result = item["result"] if isinstance(item["result"], dict) else {}
    if result.get("simulated") or result.get("outcome") == "simulated":
        raise HyperliquidError("Simulated cancellations have no exchange result to reconcile")
    status = backend.order_status(target)
    if status.get("found"):
        identity = str(status.get("cliOrdId", "")).lower() if target.startswith("0x") else str(status.get("order_id"))
        if identity != target or status.get("symbol") != item["symbols"][target]:
            raise HyperliquidError("Cancellation readback identity mismatch")
    evidence = {"requestId": request_id, "target": target, "status": status,
                "state": "observed" if status.get("found") and not status.get("uncertain") else "unknown",
                "checkedAt": datetime.now(timezone.utc).isoformat(), "canReplace": False}
    db.save_write_reconciliation(request_id, evidence, target=target)
    return evidence


def reconcile_leverage(db, backend, request_id, body, intent):
    action, expiry = intent.get("action", {}), intent.get("expiresAfter")
    if (not body or type(expiry) is not int or not 0 < expiry < 2**64 or
            type(body.get("leverage")) is not int or body["leverage"] < 1 or type(body.get("cross")) is not bool or
            type(action.get("asset")) is not int or action.get("type") != "updateLeverage" or
            action.get("leverage") != body.get("leverage") or action.get("isCross") is not body.get("cross")):
        raise HyperliquidError("Prepared leverage identity is invalid")
    saved = db.venue_recovery_state(request_id, backend.network, backend.account_address)
    if saved["result"].get("outcome") in {"simulated", "rejected", "confirmed"}:
        raise HyperliquidError("This leverage request already has a known outcome")
    symbol = str(body.get("symbol") or "").strip().upper()
    # An observed setting alone cannot rule out a delayed write. Wait until the
    # exchange's book clock has passed this persisted, signed request deadline.
    book = backend.orderbook(symbol, fresh=True)
    stamp = book.get("time")
    if type(stamp) not in (int, float) or not -5000 <= time.time() * 1000 - stamp <= 15000:
        raise HyperliquidError("Fresh exchange time is required for leverage recovery")
    if not stamp > expiry + 2000:
        raise HyperliquidError("Leverage request may still be in flight. Wait for its expiry before checking again")
    capacity = backend.trading_capacity(symbol)
    instrument = backend.markets()[symbol]["instrument"]
    expected = {"value": body["leverage"], "type": "cross" if body["cross"] else "isolated"}
    if action.get("asset") != instrument["assetId"] or capacity["leverage"] != expected:
        raise HyperliquidError("Requested leverage is not observed. The request remains unresolved; do not retry it")
    evidence = {"kind": "leverage", "requestId": request_id, "state": "current", "exchange": "hyperliquid",
                "outcome": "reconciled", "capacity": capacity, "expiresAfter": expiry, "exchangeTime": stamp, "canReplace": False}
    db.save_write_reconciliation(request_id, evidence)
    return evidence


def reconcile(db, backend, request_id):
    if not backend.account_configured:
        raise HyperliquidError("Hyperliquid account is not configured")
    body = db.venue_write_request(request_id, backend.network, backend.account_address)
    action = db.venue_prepared_order(request_id, backend.network, backend.account_address)
    if isinstance(action, dict) and action.get("type") == "chartIntent":
        return hyperliquid_chart.reconcile(db, backend, request_id, body, action)
    if isinstance(action, dict) and action.get("type") == "leverageIntent":
        return reconcile_leverage(db, backend, request_id, body, action)
    if isinstance(action, dict) and isinstance(action.get("orders"), list) and len(action["orders"]) > 1:
        try:
            return hyperliquid_batch_recovery.advance(db, backend, request_id)
        except ValueError as exc:
            raise HyperliquidError(str(exc)) from exc
    if not body or not body.get("cloid"):
        raise HyperliquidError("No recoverable client id for this account and request")
    status = backend.order_status(body["cloid"])
    if (not status.get("found") or status.get("uncertain") or
            status.get("symbol") != body.get("symbol") or
            str(status.get("cliOrdId", "")).lower() != body["cloid"].lower()):
        raise HyperliquidError("Submission remains unresolved; exchange identity is not confirmed")
    evidence = {"requestId": request_id, "cloid": body["cloid"], "status": status,
                "outcome": "reconciled", "exchange": "hyperliquid", "state": "current"}
    db.save_write_reconciliation(request_id, evidence)
    return evidence
