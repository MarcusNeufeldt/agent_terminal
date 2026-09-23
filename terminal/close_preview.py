"""What a market close would actually return right now. Python twin of
frontend/src/close-preview.js; keep the two in step (test_close_preview.py pins the
same cases). Walks the book on the side the position closes into for its full size,
subtracts the taker fee, and adds funding that settles on close.
"""

from __future__ import annotations

from typing import Any

# Kraken: measured 5.0bp on 97% of this account's fills (2026-09).
# Hyperliquid: base tier 0 (4.5bp); staking or referral discounts are not applied.
TAKER_FEE = {"kraken": 0.0005, "hyperliquid": 0.00045}


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def sorted_levels(rows: Any, side: str) -> list[tuple[float, float]]:
    """Best price first. Never trust the feed's order: Kraken returns bids worst-first."""
    levels = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            px, sz = _num(row[0]), _num(row[1])
        elif isinstance(row, dict):
            px, sz = _num(row.get("px", row.get("price"))), _num(row.get("sz", row.get("size", row.get("qty"))))
        else:
            continue
        if px and px > 0 and sz and sz > 0:
            levels.append((px, sz))
    return sorted(levels, key=lambda level: -level[0] if side == "bids" else level[0])


def close_preview(position: dict[str, Any], book: dict[str, Any], *, fee_rate: float,
                  contract_size: float = 1.0, funding: float = 0.0) -> dict[str, Any] | None:
    side = str(position.get("side") or "").lower()
    qty, entry, mult = _num(position.get("size")), _num(position.get("price")), _num(contract_size)
    if side not in {"long", "short"} or not qty or qty <= 0 or not entry or entry <= 0 or not mult or mult <= 0:
        return None
    book_side = "bids" if side == "long" else "asks"
    levels = sorted_levels((book or {}).get(book_side), book_side)
    if not levels:
        return None
    best = levels[0][0]
    left, cost, used, worst = qty, 0.0, 0, None
    for px, sz in levels:
        if left <= 1e-12:
            break
        take = min(left, sz)
        cost += take * px
        left -= take
        used += 1
        worst = px
    filled = qty - max(0.0, left)
    rest = max(0.0, left)
    direction = 1 if side == "long" else -1
    avg = cost / filled
    fund = _num(funding) or 0.0
    walked = direction * filled * mult * (avg - entry)
    exit_fee = fee_rate * filled * mult * avg
    net = walked - exit_fee + fund
    # Size beyond the visible book is priced at the worst visible level.
    if rest > 1e-12:
        net += direction * rest * mult * (worst - entry) - fee_rate * rest * mult * worst
    return {
        "netIfClosed": net,
        "grossAtBest": direction * qty * mult * (best - entry),
        "bookWalkCost": direction * filled * mult * (best - avg),
        "exitFee": exit_fee + (fee_rate * rest * mult * worst if rest > 1e-12 else 0.0),
        "fundingOnClose": fund,
        "bestPrice": best,
        "avgExitPrice": avg,
        "levelsUsed": used,
        "beyondVisibleBook": rest,
    }
