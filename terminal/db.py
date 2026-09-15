"""SQLite persistence for the terminal.

Storage only — no LLM calls here. One connection guarded by a lock is plenty
for a single user; WAL mode keeps the polling threads reading while writes
happen. The primary "Trading" session is created on first use and lives
forever; compaction state rides on the session row.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("TERMINAL_DB_PATH") or Path(__file__).resolve().parent / "terminal.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT 'Trading',
    symbol TEXT,
    summary TEXT NOT NULL DEFAULT '',
    memory TEXT NOT NULL DEFAULT '',
    context_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    billed_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    meta TEXT,
    in_context INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS actions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    armed INTEGER NOT NULL,
    actions_json TEXT NOT NULL,
    results_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS write_requests (
    request_id TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    state TEXT NOT NULL,
    response_status INTEGER,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS write_reconciliations (
    request_id TEXT PRIMARY KEY REFERENCES write_requests(request_id),
    evidence_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hyperliquid_prepared_orders (
    request_id TEXT PRIMARY KEY REFERENCES write_requests(request_id),
    action_json TEXT NOT NULL,
    prepared_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hyperliquid_fills (
    network TEXT NOT NULL,
    account TEXT NOT NULL,
    tid TEXT NOT NULL,
    oid TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL,
    PRIMARY KEY (network, account, tid, oid)
);
CREATE INDEX IF NOT EXISTS idx_hyperliquid_fills_order ON hyperliquid_fills(network, account, oid);
CREATE TABLE IF NOT EXISTS hyperliquid_fill_scans (
    network TEXT NOT NULL,
    account TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    state TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    PRIMARY KEY (network, account, start_ms, end_ms)
);
CREATE TABLE IF NOT EXISTS tp_cleanup (
    symbol TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grid_moves (
    operation_id TEXT PRIMARY KEY,
    grid_id TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts INTEGER PRIMARY KEY,
    balance REAL,
    unrealized REAL,
    equity REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS protection_alerts (
    symbol TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, kind)
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


class Database:
    def __init__(self, path: Path = DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        try:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN memory TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN context_tokens INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN prompt_tokens INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN billed_tokens INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    # ---- sessions ----

    def ensure_session(self) -> dict[str, Any]:
        """Get or create the single primary Trading session."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
                    ("Trading", _now(), _now()),
                )
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM sessions ORDER BY id LIMIT 1").fetchone()
            return dict(row)

    def set_session_summary(self, session_id: int, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET summary = ?, updated_at = ? WHERE id = ?",
                (summary, _now(), session_id),
            )
            self._conn.commit()

    def get_session_memory(self, session_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT memory FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return (row["memory"] if row else "") or ""

    def set_session_memory(self, session_id: int, memory: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET memory = ?, updated_at = ? WHERE id = ?",
                (memory[:4000], _now(), session_id),
            )
            self._conn.commit()

    def set_context_tokens(self, session_id: int, tokens: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET context_tokens = ? WHERE id = ?", (tokens, session_id)
            )
            self._conn.commit()

    def get_context_tokens(self, session_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT context_tokens FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return int(row["context_tokens"] if row else 0)

    def record_model_usage(self, session_id: int, prompt_tokens: int, billed_tokens: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET prompt_tokens = ?, context_tokens = ?, "
                "billed_tokens = billed_tokens + ? WHERE id = ?",
                (max(0, prompt_tokens), max(0, prompt_tokens), max(0, billed_tokens), session_id),
            )
            self._conn.commit()

    def add_billed_tokens(self, session_id: int, billed_tokens: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET billed_tokens = billed_tokens + ? WHERE id = ?",
                (max(0, billed_tokens), session_id),
            )
            self._conn.commit()

    def get_token_usage(self, session_id: int) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT prompt_tokens, billed_tokens FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return {
            "prompt_tokens": int(row["prompt_tokens"] if row else 0),
            "billed_tokens": int(row["billed_tokens"] if row else 0),
        }

    def get_session_summary(self, session_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT summary FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return (row["summary"] if row else "") or ""

    def upsert_equity(self, ts: int, balance: float, unrealized: float, equity: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO equity_snapshots (ts, balance, unrealized, equity) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ts) DO UPDATE SET balance=excluded.balance, unrealized=excluded.unrealized, equity=excluded.equity",
                (ts, balance, unrealized, equity),
            )
            self._conn.commit()

    def get_equity(self, limit: int = 30000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, balance, unrealized, equity FROM equity_snapshots ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"t": r["ts"], "balance": r["balance"], "unrealized": r["unrealized"], "equity": r["equity"]}
            for r in reversed(rows)
        ]

    def reset_session(self, session_id: int) -> None:
        """Wipe the conversation: all messages out of context, summary cleared.
        The memory file (sessions.memory) is kept — it reloads with the next message."""
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET in_context = 0 WHERE session_id = ?", (session_id,)
            )
            self._conn.execute(
                "UPDATE sessions SET summary = '', context_tokens = 0, prompt_tokens = 0, billed_tokens = 0, updated_at = ? WHERE id = ?",
                (_now(), session_id),
            )
            self._conn.commit()

    # ---- messages ----

    def add_message(self, session_id: int, role: str, content: str, meta: dict | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, json.dumps(meta) if meta else None, _now()),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
            )
            self._conn.commit()
            return cur.lastrowid

    def get_messages(self, session_id: int, limit: int = 60) -> list[dict[str, Any]]:
        """In-context messages, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND in_context = 1 ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [self._row_to_message(r) for r in reversed(rows)]

    def count_in_context(self, session_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND in_context = 1",
                (session_id,),
            ).fetchone()
        return row["n"]

    def compaction_candidates(self, session_id: int, keep_last: int = 20) -> tuple[list[dict[str, Any]], str]:
        """Select old messages without changing their context state."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND in_context = 1 ORDER BY id",
                (session_id,),
            ).fetchall()
            session = self._conn.execute(
                "SELECT summary FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        candidates = rows[:-max(1, keep_last)] if len(rows) > max(1, keep_last) else []
        return [self._row_to_message(row) for row in candidates], (session["summary"] if session else "") or ""

    def commit_compaction(
        self,
        session_id: int,
        message_ids: list[int],
        expected_summary: str,
        summary: str,
    ) -> bool:
        """Atomically store a summary and hide exactly the selected messages."""
        if not message_ids:
            return False
        placeholders = ",".join("?" for _ in message_ids)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                session = self._conn.execute(
                    "SELECT summary FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                count = self._conn.execute(
                    f"SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND in_context = 1 AND id IN ({placeholders})",
                    (session_id, *message_ids),
                ).fetchone()["n"]
                if not session or (session["summary"] or "") != expected_summary or count != len(message_ids):
                    self._conn.rollback()
                    return False
                self._conn.execute(
                    f"UPDATE messages SET in_context = 0 WHERE session_id = ? AND id IN ({placeholders})",
                    (session_id, *message_ids),
                )
                self._conn.execute(
                    "UPDATE sessions SET summary = ?, updated_at = ? WHERE id = ?",
                    (summary, _now(), session_id),
                )
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
        meta = None
        if row["meta"]:
            try:
                meta = json.loads(row["meta"])
            except json.JSONDecodeError:
                meta = None
        return {"role": row["role"], "content": row["content"], "meta": meta, "id": row["id"]}

    # ---- write request idempotency ----

    def claim_write_request(self, request_id: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        with self._lock:
            row = self._conn.execute(
                "SELECT endpoint, payload_json, state, response_status, result_json FROM write_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row:
                if row["endpoint"] != endpoint or row["payload_json"] != payload_json:
                    return {"state": "conflict"}
                if row["state"] == "completed" and row["result_json"]:
                    try:
                        result = json.loads(row["result_json"])
                    except json.JSONDecodeError:
                        return {"state": "pending"}
                    return {"state": "replay", "status": int(row["response_status"] or 200), "result": result}
                return {"state": "pending"}
            if endpoint in {"/api/order", "/api/grid", "/api/leverage", "/api/chart-order"} and payload.get("venue") == "hyperliquid":
                unresolved = self._venue_unresolved(payload["network"], payload["account"], limit=1)
                if unresolved:
                    return {"state": "blocked", "requestId": unresolved[0]["requestId"]}
            now = _now()
            self._conn.execute(
                "INSERT INTO write_requests (request_id, endpoint, payload_json, state, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (request_id, endpoint, payload_json, now, now),
            )
            self._conn.commit()
            return {"state": "new"}

    def _venue_unresolved(self, network: str, account: str, limit: int = 101) -> list[dict[str, Any]]:
        """Caller holds the database lock. Reconciliation never overwrites the original receipt."""
        rows = self._conn.execute(
            "SELECT w.request_id, w.endpoint, w.payload_json, w.state, w.result_json, w.created_at "
            "FROM write_requests w LEFT JOIN write_reconciliations r ON r.request_id=w.request_id "
            "WHERE w.endpoint IN ('/api/order','/api/grid','/api/leverage','/api/chart-order') "
            "AND (r.request_id IS NULL OR COALESCE(json_extract(CASE WHEN json_valid(r.evidence_json) "
            "THEN r.evidence_json ELSE '{}' END, '$.outcome'), '')!='reconciled') "
            "AND json_extract(w.payload_json, '$.venue')='hyperliquid' "
            "AND json_extract(w.payload_json, '$.network')=? "
            "AND json_extract(w.payload_json, '$.account')=? "
            "AND (w.state!='completed' OR w.result_json IS NULL OR NOT json_valid(w.result_json) "
            "OR COALESCE(json_extract(CASE WHEN json_valid(w.result_json) THEN w.result_json ELSE '{}' END, '$.outcome'), '') "
            "NOT IN ('confirmed','rejected','simulated')) ORDER BY w.rowid ASC LIMIT ?",
            (network, account.lower(), limit),
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            result.append({"requestId": row["request_id"], "endpoint": row["endpoint"], "body": payload["body"],
                           "cloid": payload["body"].get("cloid"), "outcome": "unknown",
                           "uncertain": True, "createdAt": row["created_at"]})
        return result

    def venue_unresolved(self, network: str, account: str) -> dict[str, Any]:
        with self._lock:
            items = self._venue_unresolved(network, account)
        return {"items": items[:100], "hasMore": len(items) > 100}

    def venue_write_request(self, request_id: str, network: str, account: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM write_requests WHERE request_id=? AND endpoint IN ('/api/order','/api/grid','/api/leverage','/api/chart-order')",
                (request_id,),
            ).fetchone()
        payload = json.loads(row["payload_json"]) if row else {}
        if (payload.get("venue"), payload.get("network"), payload.get("account")) != (
                "hyperliquid", network, account.lower()):
            return None
        return payload["body"]

    def prepare_hyperliquid_order(self, request_id: str, action: dict[str, Any]) -> None:
        encoded = json.dumps(action, separators=(",", ":"), allow_nan=False)
        with self._lock, self._conn:
            request = self._conn.execute(
                "SELECT state, payload_json FROM write_requests WHERE request_id=? AND endpoint IN ('/api/order','/api/grid','/api/leverage','/api/chart-order')",
                (request_id,),
            ).fetchone()
            if not request or request["state"] != "pending" or json.loads(request["payload_json"]).get("venue") != "hyperliquid":
                raise ValueError("No pending Hyperliquid request for prepared order")
            previous = self._conn.execute(
                "SELECT action_json FROM hyperliquid_prepared_orders WHERE request_id=?", (request_id,),
            ).fetchone()
            if previous:
                if previous[0] != encoded:
                    raise ValueError("Prepared order identity changed")
                return
            self._conn.execute("INSERT INTO hyperliquid_prepared_orders VALUES (?, ?, ?)",
                               (request_id, encoded, _now()))

    def venue_prepared_order(self, request_id: str, network: str, account: str) -> dict[str, Any] | None:
        if self.venue_write_request(request_id, network, account) is None:
            return None
        with self._lock:
            row = self._conn.execute("SELECT action_json FROM hyperliquid_prepared_orders WHERE request_id=?", (request_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def venue_recovery_state(self, request_id, network, account):
        if self.venue_write_request(request_id, network, account) is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT w.result_json, r.evidence_json FROM write_requests w "
                "LEFT JOIN write_reconciliations r ON r.request_id=w.request_id WHERE w.request_id=?", (request_id,),
            ).fetchone()
        result = {}
        for key, value in zip(("result", "evidence"), row):
            try:
                decoded = json.loads(value or '{}')
                result[key] = decoded if isinstance(decoded, dict) else {}
            except json.JSONDecodeError:
                result[key] = {}
        return result

    def save_batch_observation(self, request_id, cloid, observation):
        """Merge one validated readback without releasing a partially known batch."""
        with self._lock:
            prepared = self._conn.execute("SELECT action_json FROM hyperliquid_prepared_orders WHERE request_id=?", (request_id,)).fetchone()
            orders = json.loads(prepared[0]).get("orders") if prepared else None
            if not isinstance(orders, list) or not 2 <= len(orders) <= 20 or any(not isinstance(order, dict) for order in orders):
                raise ValueError("No prepared batch for reconciliation")
            expected = [order.get("c") for order in orders]
            if any(not isinstance(value, str) for value in expected) or len(set(expected)) != len(expected) or cloid not in expected:
                raise ValueError("Invalid prepared batch identities")
            previous = self._conn.execute("SELECT evidence_json FROM write_reconciliations WHERE request_id=?", (request_id,)).fetchone()
            previous = json.loads(previous[0]) if previous else {}
            targets = previous.get("targets", {})
            if not isinstance(targets, dict) or any(key not in expected or not isinstance(value, dict) for key, value in targets.items()):
                raise ValueError("Invalid existing batch reconciliation evidence")
            if observation.get("state") == "observed":
                def identity(value):
                    return str(value.get("status", {}).get("order_id", value.get("row", {}).get("oid")))
                oid = identity(observation)
                if not oid.isascii() or not oid.isdigit() or len(oid) > 20 or not 0 < int(oid) < 2**64:
                    raise ValueError("Observed batch target requires an exact exchange order id")
                if any(key != cloid and value.get("state") == "observed" and identity(value) == oid for key, value in targets.items()):
                    observation = {**observation, "state": "unknown", "error": "Duplicate exchange order identity across batch targets"}
            # A delayed failed read must not erase an already verified submission identity.
            if targets.get(cloid, {}).get("state") not in {"observed", "rejected"}:
                targets[cloid] = observation
            complete = all(targets.get(key, {}).get("state") in {"observed", "rejected"} for key in expected)
            evidence = {"requestId": request_id, "kind": "batch", "batch": True, "targets": targets,
                        "outcome": "reconciled" if complete else "unknown", "canReplace": False,
                        "remaining": sum(targets.get(key, {}).get("state") not in {"observed", "rejected"} for key in expected)}
            self._conn.execute(
                "INSERT INTO write_reconciliations VALUES (?, ?, ?) ON CONFLICT(request_id) DO UPDATE SET "
                "evidence_json=excluded.evidence_json, updated_at=excluded.updated_at",
                (request_id, json.dumps(evidence, allow_nan=False), _now()),
            )
            self._conn.commit()
            return evidence

    def venue_cancellations(self, network: str, account: str, request_id=None) -> dict[str, Any]:
        with self._lock:
            records = self._conn.execute(
                "SELECT w.request_id, w.payload_json, w.result_json, w.created_at, r.evidence_json "
                "FROM write_requests w LEFT JOIN write_reconciliations r ON r.request_id=w.request_id "
                "WHERE w.endpoint='/api/cancel' AND json_extract(w.payload_json, '$.venue')='hyperliquid' "
                "AND json_extract(w.payload_json, '$.network')=? AND json_extract(w.payload_json, '$.account')=? "
                "AND (? IS NULL OR w.request_id=?) ORDER BY w.rowid DESC LIMIT 21",
                (network, account.lower(), request_id, request_id),
            ).fetchall()
        items = []
        for row in records[:20]:
            try:
                result = json.loads(row['result_json'] or '{}')
            except json.JSONDecodeError:
                result = {}
            items.append({"requestId": row['request_id'], "body": json.loads(row['payload_json'])["body"],
                          "result": result, "createdAt": row['created_at'],
                          "evidence": json.loads(row['evidence_json'] or '{}')})
        return {"items": items, "hasMore": len(records) > 20}

    def save_write_reconciliation(self, request_id: str, evidence: dict[str, Any], *, target=None) -> None:
        with self._lock:
            if target is not None:
                row = self._conn.execute("SELECT evidence_json FROM write_reconciliations WHERE request_id=?", (request_id,)).fetchone()
                previous = json.loads(row[0]) if row else {}
                evidence = {"kind": "cancel", "targets": {**previous.get("targets", {}), target: evidence}}
            self._conn.execute(
                "INSERT INTO write_reconciliations VALUES (?, ?, ?) "
                "ON CONFLICT(request_id) DO UPDATE SET evidence_json=excluded.evidence_json, updated_at=excluded.updated_at",
                (request_id, json.dumps(evidence, allow_nan=False), _now()),
            )
            self._conn.commit()

    def complete_write_request(self, request_id: str, status: int, result: Any) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE write_requests SET state = 'completed', response_status = ?, result_json = ?, updated_at = ? "
                "WHERE request_id = ? AND state = 'pending'",
                (status, json.dumps(result, default=str), _now(), request_id),
            )
            self._conn.commit()

    def save_hyperliquid_fill_page(self, network, account, fills, start, end, state) -> int:
        account = account.lower()
        inserted = 0
        with self._lock, self._conn:
            for fill in fills:
                identity = (network, account, fill["id"], fill["orderId"])
                encoded = json.dumps(fill, sort_keys=True, separators=(",", ":"), allow_nan=False)
                previous = self._conn.execute(
                    "SELECT payload_json FROM hyperliquid_fills WHERE network=? AND account=? AND tid=? AND oid=?",
                    identity,
                ).fetchone()
                if previous:
                    if previous[0] != encoded:
                        raise ValueError("Conflicting persisted Hyperliquid fill; page rolled back")
                    continue
                self._conn.execute("INSERT INTO hyperliquid_fills VALUES (?, ?, ?, ?, ?, ?)",
                                   (*identity, encoded, fill["time"]))
                inserted += 1
            self._conn.execute(
                "INSERT INTO hyperliquid_fill_scans VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(network, account, start_ms, end_ms) DO UPDATE SET state=excluded.state, scanned_at=excluded.scanned_at",
                (network, account, start, end, state, _now()),
            )
        return inserted

    def hyperliquid_order_fills(self, network, account, order_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM hyperliquid_fills WHERE network=? AND account=? AND oid=? ORDER BY timestamp_ms, tid",
                (network, account.lower(), order_id),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    # ---- actions log ----

    def claim_action_proposal(self, session_id: int, message_id: int, block_index: int, expected: list) -> bool:
        """Atomically consume a proposal card; duplicate Execute requests then fail closed."""
        with self._lock:
            row = self._conn.execute(
                "SELECT meta FROM messages WHERE id = ? AND session_id = ?",
                (message_id, session_id),
            ).fetchone()
            if not row or not row["meta"]:
                return False
            try:
                meta = json.loads(row["meta"])
                blocks = meta.get("actionProposals") or []
            except (TypeError, json.JSONDecodeError):
                return False
            if block_index < 0 or block_index >= len(blocks) or blocks[block_index] != expected:
                return False
            blocks.pop(block_index)
            meta["actionProposals"] = blocks
            self._conn.execute("UPDATE messages SET meta = ? WHERE id = ?", (json.dumps(meta), message_id))
            self._conn.commit()
            return True

    def log_action(self, mode: str, armed: bool, actions: list, results: list) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO actions_log (ts, mode, armed, actions_json, results_json) VALUES (?, ?, ?, ?, ?)",
                (_now(), mode, 1 if armed else 0,
                 json.dumps(actions, default=str), json.dumps(results, default=str)),
            )
            self._conn.commit()

    def tp_cleanup_states(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT payload_json FROM tp_cleanup").fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_tp_cleanup(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tp_cleanup VALUES (?, ?) ON CONFLICT(symbol) DO UPDATE SET payload_json=excluded.payload_json",
                (state["symbol"], json.dumps(state)),
            )
            self._conn.commit()

    def recorded_grids(self, symbol: str, side: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, actions_json, results_json FROM actions_log WHERE armed = 1 ORDER BY id DESC LIMIT 500"
            ).fetchall()
        grids = []
        for row in rows:
            for index, (action, result) in enumerate(zip(json.loads(row["actions_json"]), json.loads(row["results_json"]))):
                if action.get("type") != "ladder" or result.get("symbol") != symbol or result.get("side") != side:
                    continue
                orders = [{**r["order"], "orderId": r["exchangeId"]} for r in result.get("responses", [])
                          if r.get("outcome") == "confirmed" and r.get("exchangeId") and r.get("order")]
                if orders:
                    grids.append({"gridId": f"{row['id']}:{index}", "symbol": symbol, "side": side,
                                  "settings": action, "orders": orders})
        return grids

    def grid_move(self, operation_id: str = "", grid_id: str = "") -> dict[str, Any] | None:
        with self._lock:
            if operation_id:
                row = self._conn.execute("SELECT payload_json FROM grid_moves WHERE operation_id = ?", (operation_id,)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT payload_json FROM grid_moves WHERE grid_id = ? AND state != 'confirmed' ORDER BY rowid DESC LIMIT 1", (grid_id,)
                ).fetchone()
        return json.loads(row[0]) if row else None

    def save_grid_move(self, move: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO grid_moves VALUES (?, ?, ?, ?) ON CONFLICT(operation_id) DO UPDATE SET state=excluded.state, payload_json=excluded.payload_json",
                (move["operationId"], move["gridId"], move["outcome"], json.dumps(move)),
            )
            self._conn.commit()

    def inferred_managed_protection_ids(self) -> set[str]:
        """Successful legacy replace_tp/replace_sl orders were full-position protection."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT actions_json, results_json FROM actions_log WHERE armed = 1 ORDER BY id"
            ).fetchall()
        latest: dict[tuple[str, str], str] = {}
        for row in rows:
            try:
                actions = json.loads(row["actions_json"])
                results = json.loads(row["results_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            for action, result in zip(actions, results):
                kind = str(action.get("type") or "")
                if kind not in {"replace_tp", "replace_sl"} or result.get("ok") is not True:
                    continue
                send = (result.get("response") or {}).get("sendStatus") or {}
                order_id = send.get("order_id") or send.get("orderId")
                if order_id:
                    latest[(str(action.get("symbol") or ""), kind)] = str(order_id)
        return set(latest.values())

    # ---- persistent protection alerts ----

    def set_protection_alert(self, symbol: str, kind: str, payload: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO protection_alerts (symbol, kind, status, payload, updated_at) VALUES (?, ?, 'UNPROTECTED', ?, ?) "
                "ON CONFLICT(symbol, kind) DO UPDATE SET status = excluded.status, payload = excluded.payload, updated_at = excluded.updated_at",
                (symbol, kind, json.dumps(payload, default=str), _now()),
            )
            self._conn.commit()

    def clear_protection_alert(self, symbol: str, kind: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM protection_alerts WHERE symbol = ? AND kind = ?", (symbol, kind))
            self._conn.commit()

    def protection_alerts(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, kind, status, payload, updated_at FROM protection_alerts ORDER BY updated_at DESC"
            ).fetchall()
        alerts = []
        for row in rows:
            try:
                details = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                details = {"message": str(row["payload"] or "")}
            alerts.append({
                "symbol": row["symbol"], "kind": row["kind"], "status": row["status"],
                "details": details, "updatedAt": row["updated_at"],
            })
        return alerts

    # ---- events ----

    def log_event(self, kind: str, payload: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
                (_now(), kind, json.dumps(payload, default=str)),
            )
            self._conn.commit()

    def latest_chase_snapshots(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM events WHERE kind = 'chase' ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            chase_id = str(payload.get("id") or "") if isinstance(payload, dict) else ""
            if chase_id and chase_id not in latest:
                latest[chase_id] = payload
        return list(latest.values())
