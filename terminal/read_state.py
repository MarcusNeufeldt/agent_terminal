"""Preserve unavailable versus current account and order-book state."""

from __future__ import annotations

from typing import Any


def rows_payload(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    failure = next((row for row in rows if isinstance(row, dict) and row.get("error")), None)
    if failure:
        last_known = failure.get("lastKnown")
        return {
            name: last_known if isinstance(last_known, list) else [],
            "state": "unavailable",
            "error": failure["error"],
            "ageSeconds": failure.get("ageSeconds"),
        }
    return {name: rows, "state": "current", "ageSeconds": 0}


def account_payload(account: dict[str, Any]) -> dict[str, Any]:
    if not account.get("error"):
        return {**account, "state": "current", "ageSeconds": 0}
    last_known = account.get("lastKnown")
    return {
        **(last_known if isinstance(last_known, dict) else {}),
        "state": "unavailable",
        "error": account["error"],
        "ageSeconds": account.get("ageSeconds"),
    }
