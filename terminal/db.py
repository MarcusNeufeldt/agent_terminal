"""SQLite persistence for the terminal.

Storage only — no LLM calls here. One connection guarded by a lock is plenty
for a single user; WAL mode keeps the polling threads reading while writes
happen. The primary "Trading" session is created on first use and lives
forever; compaction state rides on the session row.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "terminal.db"

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
                "UPDATE sessions SET summary = '', context_tokens = 0, updated_at = ? WHERE id = ?",
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

    def compact(self, session_id: int, keep_last: int = 20) -> list[dict[str, Any]] | None:
        """Mark all but the newest `keep_last` messages out of context.

        Returns the messages selected for summarization (the caller generates
        the summary and stores it via set_session_summary), or None if nothing
        needs compacting.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? AND in_context = 1 ORDER BY id",
                (session_id,),
            ).fetchall()
            if len(rows) <= keep_last:
                return None
            to_compact = rows[:-keep_last]
            ids = [r["id"] for r in to_compact]
            self._conn.executemany(
                "UPDATE messages SET in_context = 0 WHERE id = ?", [(i,) for i in ids]
            )
            self._conn.commit()
        return [self._row_to_message(r) for r in to_compact]

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> dict[str, Any]:
        meta = None
        if row["meta"]:
            try:
                meta = json.loads(row["meta"])
            except json.JSONDecodeError:
                meta = None
        return {"role": row["role"], "content": row["content"], "meta": meta, "id": row["id"]}

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
