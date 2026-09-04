"""Kraken Futures account history: /api/history/v2/account-log.

Full account log with pagination (the plain /fills endpoint only returns the
last 100 fills). Every row: date, info ("futures trade", "funding rate change",
"interest payment", ...), contract, realized_pnl, realized_funding, fee.
Signed with the same futures API key; module-level cache (the log is append-only).
"""

from __future__ import annotations

import json
import threading
import time
from urllib import request

CACHE_TTL = 300.0
_page_cache: list[dict] | None = None
_cache_ts = 0.0
_lock = threading.Lock()


def _get(client, path: str, params: str = "") -> dict:
    url = f"{client.base_url.rstrip('/')}{path}" + (f"?{params}" if params else "")
    headers = {"Accept": "application/json", "User-Agent": "kraken-terminal/1.0"}
    headers.update(client._auth_headers_for_path(path, params))
    req = request.Request(url, headers=headers, method="GET")
    with request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def full_log(client, force: bool = False) -> list[dict]:
    """Every account-log row, oldest first. Cursors are epoch MILLISECONDS —
    ISO date strings are silently ignored by this endpoint."""
    global _page_cache, _cache_ts
    with _lock:
        if not force and _page_cache is not None and time.time() - _cache_ts < CACHE_TTL:
            return _page_cache

    rows: list[dict] = []
    since: int | None = None
    seen_ids: set[int] = set()
    failure: Exception | None = None
    while True:
        params = "count=1000&sort=asc"
        if since:
            params += f"&since={since}"
        try:
            r = _get(client, "/api/history/v2/account-log", params)
        except Exception as exc:
            failure = exc
            break
        logs = r.get("logs") or []
        fresh = [l for l in logs if l.get("id") not in seen_ids]
        if not fresh:
            break
        seen_ids.update(l.get("id") for l in fresh)
        rows.extend(fresh)
        last = max(_row_ms(l) for l in fresh)
        since = last + 1
        if len(logs) < 1000:
            break

    if failure is not None:
        raise RuntimeError(f"account log pagination failed after {len(rows)} rows: {failure}") from failure

    rows.sort(key=lambda l: (l.get("date") or "", l.get("id") or 0))
    with _lock:
        _page_cache = rows
        _cache_ts = time.time()
    return rows


def _row_ms(row: dict) -> int:
    import calendar
    d = str(row.get("date") or "")
    try:
        tt = time.strptime(d[:19], "%Y-%m-%dT%H:%M:%S")
        return int(calendar.timegm(tt) * 1000)
    except ValueError:
        return 0


def compact_rows(client) -> list[dict]:
    # Projected rows for stats: {t (epoch s), info, contract, pnl, funding, fee}.
    import calendar
    out = []
    for r in full_log(client):
        d = str(r.get("date") or "")
        try:
            t = int(calendar.timegm(time.strptime(d[:19], "%Y-%m-%dT%H:%M:%S")))
        except ValueError:
            continue
        out.append({
            "t": t,
            "info": r.get("info"),
            "contract": r.get("contract"),
            "pnl": r.get("realized_pnl"),
            "funding": r.get("realized_funding"),
            "fee": r.get("fee"),
        })
    return out


def trade_history(client, symbol: str, limit: int = 50, force: bool = False) -> list[dict]:
    """Recent executions for one contract, reconstructed from paired account-log rows."""
    wanted = symbol.strip().lower()
    limit = max(1, min(100, int(limit)))
    executions: dict[str, dict] = {}
    for row in full_log(client, force=force):
        contract = str(row.get("contract") or "").lower()
        execution = str(row.get("execution") or "")
        if contract != wanted or not execution or row.get("info") not in {"futures trade", "futures partial liquidation"}:
            continue
        item = executions.setdefault(execution, {
            "executionId": execution,
            "date": row.get("date"),
            "symbol": wanted.upper(),
            "tradePrice": row.get("trade_price"),
            "markPrice": row.get("mark_price"),
            "size": None,
            "side": None,
            "positionBefore": None,
            "positionAfter": None,
            "realizedPnl": 0.0,
            "realizedFunding": 0.0,
            "fee": 0.0,
            "liquidation": row.get("info") == "futures partial liquidation",
            "_id": row.get("id") or 0,
        })
        item["_id"] = max(item["_id"], row.get("id") or 0)
        if str(row.get("asset") or "").lower() == contract:
            before = row.get("old_balance")
            after = row.get("new_balance")
            if before is not None and after is not None:
                delta = float(after) - float(before)
                item["size"] = round(abs(delta), 12)
                item["side"] = "buy" if delta > 0 else "sell"
                item["positionBefore"] = before
                item["positionAfter"] = after
        item["realizedPnl"] += float(row.get("realized_pnl") or 0)
        item["realizedFunding"] += float(row.get("realized_funding") or 0)
        item["fee"] += float(row.get("fee") or 0)

    rows = sorted(executions.values(), key=lambda item: (str(item["date"]), item["_id"]), reverse=True)[:limit]
    for item in rows:
        item["realizedPnl"] = round(item["realizedPnl"], 10)
        item["realizedFunding"] = round(item["realizedFunding"], 10)
        item["fee"] = round(item["fee"], 10)
        item["netPnl"] = round(item["realizedPnl"] + item["realizedFunding"] - item["fee"], 10)
        del item["_id"]
    return rows