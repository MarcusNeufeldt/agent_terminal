"""Shared Kraken write IDs and nested-operation response parsing."""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterable


def unique_client_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def ensure_client_id(params: dict[str, Any], prefix: str) -> dict[str, Any]:
    prepared = dict(params)
    if not str(prepared.get("cliOrdId") or "").strip():
        prepared["cliOrdId"] = unique_client_id(prefix)
    return prepared


def parse_operation(
    response: Any,
    key: str,
    confirmed_statuses: str | Iterable[str],
    *,
    ambiguous_statuses: Iterable[str] = (),
) -> dict[str, Any]:
    confirmed = {confirmed_statuses} if isinstance(confirmed_statuses, str) else set(confirmed_statuses)
    ambiguous = set(ambiguous_statuses)
    base = {"response": response, "operation": key}
    if not isinstance(response, dict):
        return {**base, "outcome": "unknown", "nestedStatus": None, "error": "non-object Kraken response"}
    if str(response.get("result") or "") != "success":
        return {
            **base, "outcome": "rejected", "nestedStatus": None,
            "error": f"Kraken result {response.get('result') or 'missing'}: {json.dumps(response, default=str)[:300]}",
        }
    detail = response.get(key)
    if not isinstance(detail, dict):
        return {**base, "outcome": "unknown", "nestedStatus": None, "error": f"Kraken response missing {key}"}
    status = str(detail.get("status") or "")
    exchange_id = detail.get("order_id") or detail.get("orderId")
    if status in confirmed:
        outcome = "confirmed"
        error = None
    elif status in ambiguous:
        outcome = "unknown"
        error = f"Kraken {key} status {status or 'missing'} is ambiguous"
    else:
        outcome = "rejected"
        error = f"Kraken {key} status {status or 'missing'}"
    return {
        **base,
        "outcome": outcome,
        "nestedStatus": status or None,
        "exchangeId": str(exchange_id) if exchange_id else None,
        "detail": detail,
        **({"error": error} if error else {}),
    }
