"""The database. Final schema; no later phase adds a column.

sqlite3 with WAL. One connection per process, guarded by a lock because the
watcher and the chat loop share it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS session(
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
  state TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'chat',
  slots_json TEXT NOT NULL DEFAULT '{}',
  outcome TEXT, outcome_verb TEXT, recovery_declined_booking TEXT,
  budget_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS slot_fact(
  session_id TEXT NOT NULL, field TEXT NOT NULL, value_json TEXT,
  source TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(session_id, field));

CREATE TABLE IF NOT EXISTS approval(
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
  digest TEXT NOT NULL, payload_json TEXT NOT NULL, text_shown TEXT NOT NULL,
  state TEXT NOT NULL, consumed_at TEXT, invalidated_reason TEXT,
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS booking_intent(
  idempotency_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, digest TEXT NOT NULL,
  kind TEXT NOT NULL, hold_id TEXT, booking_id TEXT, state TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS memory(
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, key TEXT NOT NULL,
  value TEXT NOT NULL, category TEXT NOT NULL,
  source TEXT NOT NULL CHECK(source IN ('user','inferred')),
  confirmed INTEGER NOT NULL DEFAULT 0, forgotten_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_used_at TEXT);

CREATE TABLE IF NOT EXISTS memory_version(
  id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL,
  value TEXT NOT NULL, superseded_at TEXT NOT NULL, reason TEXT);

CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
  body TEXT NOT NULL, created_at TEXT NOT NULL, delivered_at TEXT);

CREATE TABLE IF NOT EXISTS event(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts TEXT NOT NULL,
  type TEXT NOT NULL, payload_json TEXT NOT NULL);

CREATE INDEX IF NOT EXISTS ix_slot_fact_session ON slot_fact(session_id);
CREATE INDEX IF NOT EXISTS ix_approval_session ON approval(session_id, state);
CREATE INDEX IF NOT EXISTS ix_memory_user ON memory(user_id);
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox(delivered_at);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ basics

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    def reconnect(self) -> None:
        """For restart tests: reopen the same file, dropping in-memory state."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass
            self._conn = sqlite3.connect(self.path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(SCHEMA)
            self._conn.commit()


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def loads(raw: str | None, default: Any = None) -> Any:
    if raw in (None, ""):
        return default
    return json.loads(raw)
