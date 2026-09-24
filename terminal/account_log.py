"""Kraken Futures account history: /api/history/v2/account-log.

Full account log with pagination (the plain /fills endpoint only returns the
last 100 fills). Every row: date, info ("futures trade", "funding rate change",
"interest payment", ...), contract, realized_pnl, realized_funding, fee.

The log is append-only, so it is fetched incrementally: rows already held (in memory
and on disk next to the terminal database) are kept, and each refresh asks only for
rows since the newest one. Re-downloading tens of thousands of rows ran into Kraken's
history rate limit (HTTP 429). A rate-limited backfill keeps what it got and resumes
from there on the next call.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from urllib import request
from urllib.error import HTTPError

CACHE_TTL = 30.0  # a refresh is usually one small request now
RATE_LIMIT_BACKOFF = (2.0, 4.0, 8.0)
_page_cache: list[dict] | None = None
_cache_ts = 0.0
_cache_identity: str | None = None
_lock = threading.Lock()
_fetch_lock = threading.Lock()


def _get(client, path: str, params: str = "") -> dict:
    url = f"{client.base_url.rstrip('/')}{path}" + (f"?{params}" if params else "")
    headers = {"Accept": "application/json", "User-Agent": "kraken-terminal/1.0"}
    headers.update(client._auth_headers_for_path(path, params))
    req = request.Request(url, headers=headers, method="GET")
    with request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _identity(client) -> str:
    # Which account a saved log belongs to, without storing the key itself.
    raw = f"{getattr(client, 'base_url', '')}|{getattr(client, 'api_key', '') or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_path() -> Path:
    db = Path(os.environ.get("TERMINAL_DB_PATH") or Path(__file__).resolve().parent / "terminal.db")
    return db.parent / "account_log_cache.json"


def _load_disk(identity: str) -> list[dict] | None:
    try:
        saved = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rows = saved.get("rows") if isinstance(saved, dict) and saved.get("identity") == identity else None
    return rows if isinstance(rows, list) else None


def _save_disk(client, identity: str, rows: list[dict]) -> None:
    if not getattr(client, "api_key", None):
        return  # no real account behind this client: nothing worth persisting
    path = _cache_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"identity": identity, "rows": rows}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # the in-memory log still works; the next restart just refetches


def _get_with_backoff(client, params: str) -> dict:
    for pause in (*RATE_LIMIT_BACKOFF, None):
        try:
            return _get(client, "/api/history/v2/account-log", params)
        except HTTPError as exc:
            if exc.code != 429 or pause is None:
                raise
            time.sleep(pause)
    raise RuntimeError("unreachable")


def cached_rows(client) -> list[dict]:
    """Whatever is held for this account, without asking Kraken. May be stale."""
    identity = _identity(client)
    with _lock:
        if _page_cache is not None and _cache_identity == identity:
            return _page_cache
    return _load_disk(identity) or []


def full_log(client, force: bool = False) -> list[dict]:
    """Every account-log row, oldest first. Cursors are epoch MILLISECONDS —
    ISO date strings are silently ignored by this endpoint. Raises when Kraken could
    not be reached, after keeping any rows it did return."""
    global _page_cache, _cache_ts, _cache_identity
    identity = _identity(client)

    def fresh_cache():
        return (not force and _page_cache is not None and _cache_identity == identity
                and time.time() - _cache_ts < CACHE_TTL)

    with _lock:
        if fresh_cache():
            return _page_cache
    with _fetch_lock:
        with _lock:
            if fresh_cache():
                return _page_cache  # another request refreshed it while this one waited
            held = list(_page_cache) if _page_cache is not None and _cache_identity == identity else None
        rows = held if held is not None else (_load_disk(identity) or [])
        seen = {row.get("id") for row in rows}
        since = max((_row_ms(row) for row in rows), default=0) or None
        added = 0
        failure: Exception | None = None
        while True:
            # The cursor includes the newest held millisecond and ids drop the repeats,
            # so rows sharing that millisecond across a page boundary are never skipped.
            params = "count=1000&sort=asc" + (f"&since={since}" if since else "")
            try:
                page = _get_with_backoff(client, params)
            except Exception as exc:
                failure = exc
                break
            logs = page.get("logs") or []
            fresh = [row for row in logs if row.get("id") not in seen]
            if not fresh:
                break
            seen.update(row.get("id") for row in fresh)
            rows.extend(fresh)
            added += len(fresh)
            since = max(_row_ms(row) for row in fresh)
            if len(logs) < 1000:
                break
            time.sleep(0.5)  # a long backfill stays under the history rate limit

        rows.sort(key=lambda row: (row.get("date") or "", row.get("id") or 0))
        if failure is None or added:
            # A failed refresh that brought nothing new never replaces what is held.
            with _lock:
                _page_cache, _cache_identity = rows, identity
                if failure is None:
                    _cache_ts = time.time()
        if added:
            _save_disk(client, identity, rows)
    if failure is not None:
        raise RuntimeError(f"account log pagination failed after {added} new rows: {failure}") from failure
    return rows


def _row_ms(row: dict) -> int:
    import calendar
    d = str(row.get("date") or "")
    try:
        tt = time.strptime(d[:19], "%Y-%m-%dT%H:%M:%S")
        return int(calendar.timegm(tt) * 1000)
    except ValueError:
        return 0


def compact_rows(client, rows: list[dict] | None = None) -> list[dict]:
    # Projected rows for stats: {t (epoch s), info, contract, pnl, funding, fee, liqFee}.
    # liqFee is the liquidation penalty, which Kraken books apart from `fee`.
    # rows: an already-held log, e.g. cached_rows() after a failed refresh.
    import calendar
    out = []
    for r in (full_log(client) if rows is None else rows):
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
            "liqFee": r.get("liquidation_fee"),
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
            "liquidationFee": 0.0,
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
        item["liquidationFee"] += float(row.get("liquidation_fee") or 0)

    rows = sorted(executions.values(), key=lambda item: (str(item["date"]), item["_id"]), reverse=True)[:limit]
    for item in rows:
        item["realizedPnl"] = round(item["realizedPnl"], 10)
        item["realizedFunding"] = round(item["realizedFunding"], 10)
        item["fee"] = round(item["fee"], 10)
        item["liquidationFee"] = round(item["liquidationFee"], 10)
        item["netPnl"] = round(item["realizedPnl"] + item["realizedFunding"] - item["fee"] - item["liquidationFee"], 10)
        del item["_id"]
    return rows