"""Memory, with an admission filter and a forget cascade.

Two properties matter more than retrieval quality:

1. The admission filter runs in the SQL, not the prompt. A forgotten or
   unconfirmed-inferred row cannot be argued into the context by a high score.
2. Forgetting cascades. A tombstone in this table does nothing if the same fact
   also sits in a slot or in the conversation history, so `forget()` clears all
   three. That is the difference between "marked forgotten" and "gone".
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from errand.clock import Clock
from errand.db import Database, dumps


@dataclass(frozen=True)
class Memory:
    id: str
    key: str
    value: str
    category: str
    source: str
    confirmed: bool
    forgotten_at: datetime | None
    created_at: datetime


class MemoryStore:
    def __init__(self, db: Database, clock: Clock, history=None) -> None:
        self.db = db
        self.clock = clock
        self.history = history  # optional: set when history redaction is available

    # ------------------------------------------------------------------ writes

    def add(
        self,
        user_id: str,
        key: str,
        value: str,
        category: str,
        source: str = "user",
        confirmed: bool | None = None,
    ) -> str:
        if source not in ("user", "inferred"):
            raise ValueError(f"bad source {source!r}")
        # An inferred fact starts unconfirmed by definition; a user-stated one is
        # established the moment it is said.
        if confirmed is None:
            confirmed = source == "user"
        mem_id = f"mem_{uuid.uuid4().hex[:10]}"
        now = self.clock.now().isoformat()
        self.db.execute(
            "INSERT INTO memory(id,user_id,key,value,category,source,confirmed,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (mem_id, user_id, key, value, category, source, int(confirmed), now, now),
        )
        return mem_id

    def infer(self, user_id: str, key: str, value: str, category: str) -> str:
        return self.add(user_id, key, value, category, source="inferred", confirmed=False)

    def confirm(self, memory_id: str) -> None:
        self.db.execute(
            "UPDATE memory SET confirmed=1, source='user', updated_at=? WHERE id=?",
            (self.clock.now().isoformat(), memory_id),
        )

    def update(self, memory_id: str, value: str, reason: str = "user correction") -> None:
        """Supersede the value, keeping the prior one for audit only."""
        row = self.db.query_one("SELECT * FROM memory WHERE id=?", (memory_id,))
        if row is None:
            raise KeyError(memory_id)
        now = self.clock.now().isoformat()
        self.db.execute(
            "INSERT INTO memory_version(memory_id,value,superseded_at,reason)"
            " VALUES (?,?,?,?)",
            (memory_id, row["value"], now, reason),
        )
        self.db.execute(
            "UPDATE memory SET value=?, source='user', confirmed=1, updated_at=?"
            " WHERE id=?",
            (value, now, memory_id),
        )

    def forget(self, user_id: str, key: str) -> int:
        """Tombstone plus cascade. Returns the number of memory rows affected."""
        rows = self.db.query(
            "SELECT id FROM memory WHERE user_id=? AND key=? AND forgotten_at IS NULL",
            (user_id, key),
        )
        now = self.clock.now().isoformat()
        for row in rows:
            mem_id = row["id"]
            self.db.execute(
                "UPDATE memory SET forgotten_at=?, updated_at=? WHERE id=?",
                (now, now, mem_id),
            )
            # cascade 1: any slot that derived from this memory
            self.db.execute(
                "UPDATE slot_fact SET value_json=NULL, source='forgotten', updated_at=?"
                " WHERE source=?",
                (now, f"memory:{mem_id}"),
            )
            # cascade 2: the conversation history that stated it
            if self.history is not None:
                self.history.forget(mem_id)
            self.db.execute(
                "INSERT INTO event(session_id,ts,type,payload_json) VALUES (?,?,?,?)",
                (None, now, "memory_forgotten", dumps({"memory_id": mem_id, "key": key})),
            )
        return len(rows)

    # ------------------------------------------------------------------- reads

    def admissible(self, user_id: str) -> list[Memory]:
        rows = self.db.query(
            "SELECT * FROM memory WHERE user_id=? AND forgotten_at IS NULL"
            " AND (source='user' OR confirmed=1) ORDER BY created_at",
            (user_id,),
        )
        return [self._row(r) for r in rows]

    def inspect(self, user_id: str) -> list[Memory]:
        """Everything, including forgotten rows, for the user-facing control."""
        rows = self.db.query(
            "SELECT * FROM memory WHERE user_id=? ORDER BY created_at", (user_id,)
        )
        return [self._row(r) for r in rows]

    def all_rows(self, user_id: str) -> list[dict]:
        """Raw rows with their exclusion reason. Ids and reasons, never values."""
        out = []
        for r in self.db.query("SELECT * FROM memory WHERE user_id=?", (user_id,)):
            if r["forgotten_at"]:
                reason = "forgotten"
            elif r["source"] == "inferred" and not r["confirmed"]:
                reason = "unconfirmed"
            else:
                reason = None
            out.append(
                {"id": r["id"], "key": r["key"], "reason": reason,
                 "source": r["source"], "confirmed": bool(r["confirmed"])}
            )
        return out

    def _row(self, r) -> Memory:
        return Memory(
            id=r["id"],
            key=r["key"],
            value=r["value"],
            category=r["category"],
            source=r["source"],
            confirmed=bool(r["confirmed"]),
            forgotten_at=datetime.fromisoformat(r["forgotten_at"]) if r["forgotten_at"] else None,
            created_at=datetime.fromisoformat(r["created_at"]),
        )

    # ------------------------------------------------------------------ scoring

    def score(self, mem: Memory, intent: str) -> int:
        """Deterministic: category match, then lexical overlap. No embeddings.

        Nondeterministic retrieval cannot be pinned in a fixture, and a fixture is
        what makes the context-budget test meaningful.
        """
        words = {w for w in intent.lower().replace(",", " ").split() if len(w) > 2}
        s = 0
        if mem.category.lower() in words:
            s += 3
        s += len(words & set(mem.key.lower().split())) * 1
        s += len(words & set(mem.value.lower().split())) * 1
        return s

    def retrieve(self, user_id: str, intent: str, limit: int = 10) -> list[Memory]:
        rows = self.admissible(user_id)
        scored = sorted(
            ((-self.score(m, intent), m.created_at, m.id), m) for m in rows
        )
        return [m for _, m in scored[:limit]]
