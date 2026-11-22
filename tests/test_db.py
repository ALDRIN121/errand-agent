"""Phase 2 gate, part 4: the schema holds what the later phases demand of it."""
from __future__ import annotations

import pytest

from errand.db import Database, dumps, loads


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "schema.db")
    yield d
    d.close()


REQUIRED_COLUMNS = {
    "session": {"id", "user_id", "state", "mode", "slots_json", "outcome",
                "outcome_verb", "recovery_declined_booking", "budget_json",
                "created_at", "updated_at"},
    "slot_fact": {"session_id", "field", "value_json", "source", "updated_at"},
    "approval": {"id", "session_id", "kind", "digest", "payload_json", "text_shown",
                 "state", "consumed_at", "invalidated_reason", "created_at"},
    "booking_intent": {"idempotency_key", "session_id", "digest", "kind", "hold_id",
                       "booking_id", "state", "created_at", "updated_at"},
    "memory": {"id", "user_id", "key", "value", "category", "source", "confirmed",
               "forgotten_at", "created_at", "updated_at", "last_used_at"},
    "memory_version": {"id", "memory_id", "value", "superseded_at", "reason"},
    "outbox": {"id", "session_id", "body", "created_at", "delivered_at"},
    "event": {"id", "session_id", "ts", "type", "payload_json"},
}


@pytest.mark.parametrize("table,columns", sorted(REQUIRED_COLUMNS.items()))
def test_table_has_required_columns(db, table, columns):
    rows = db.query(f"PRAGMA table_info({table})")
    actual = {r["name"] for r in rows}
    missing = columns - actual
    assert not missing, f"{table} is missing {sorted(missing)}"


def test_consumed_at_exists_because_the_replay_guard_needs_it(db):
    """A replay guard encoded in `state` would collide with the EXECUTING check."""
    rows = {r["name"] for r in db.query("PRAGMA table_info(approval)")}
    assert "consumed_at" in rows


def test_booking_intent_carries_kind_for_restart_reconciliation(db):
    rows = {r["name"] for r in db.query("PRAGMA table_info(booking_intent)")}
    assert "kind" in rows


def test_memory_source_is_constrained(db):
    with pytest.raises(Exception):
        db.execute(
            "INSERT INTO memory(id,user_id,key,value,category,source,confirmed,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("m1", "u", "k", "v", "c", "from_the_ether", 0, "now", "now"),
        )


def test_all_tables_write_and_read_back(db):
    now = "2026-11-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO session(id,user_id,state,mode,slots_json,budget_json,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("s1", "u1", "NEW", "chat", "{}", "{}", now, now),
    )
    db.execute(
        "INSERT INTO slot_fact(session_id,field,value_json,source,updated_at)"
        " VALUES (?,?,?,?,?)",
        ("s1", "service", dumps("plumber"), "user", now),
    )
    db.execute(
        "INSERT INTO outbox(session_id,body,created_at) VALUES (?,?,?)",
        ("s1", "message", now),
    )
    db.execute(
        "INSERT INTO event(session_id,ts,type,payload_json) VALUES (?,?,?,?)",
        ("s1", now, "test", "{}"),
    )
    assert db.query_one("SELECT * FROM session WHERE id='s1'")["state"] == "NEW"
    assert db.query_one("SELECT * FROM slot_fact WHERE session_id='s1'")["field"] == "service"
    assert db.query_one("SELECT * FROM outbox WHERE session_id='s1'")["body"] == "message"
    assert len(db.query("SELECT * FROM event")) == 1


def test_survives_close_and_reopen(tmp_path):
    path = tmp_path / "persist.db"
    d1 = Database(path)
    d1.execute(
        "INSERT INTO session(id,user_id,state,mode,slots_json,budget_json,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("s1", "u1", "SEARCHING", "chat", "{}", "{}", "t", "t"),
    )
    d1.close()

    d2 = Database(path)
    row = d2.query_one("SELECT * FROM session WHERE id='s1'")
    assert row is not None and row["state"] == "SEARCHING"
    d2.close()


def test_reconnect_reopens_the_same_file(db):
    db.execute(
        "INSERT INTO session(id,user_id,state,mode,slots_json,budget_json,created_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("s2", "u1", "PAUSED", "chat", "{}", "{}", "t", "t"),
    )
    db.reconnect()
    assert db.query_one("SELECT state FROM session WHERE id='s2'")["state"] == "PAUSED"


def test_loads_handles_null():
    assert loads(None, {}) == {}
    assert loads("", []) == []
    assert loads('{"a":1}', {}) == {"a": 1}
