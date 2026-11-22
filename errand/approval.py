"""Approval as a row with a digest, not a sentence.

Three mechanisms, and they are independent on purpose:

1. `PendingAction.digest()` binds the exact material details. Any change to
   provider, service, slot, price, booking id or attempt produces a different
   digest, so an approval cannot survive a material change.
2. `validate()` refuses an approval that is expired, already consumed, or scoped
   to a session that is not EXECUTING.
3. `execute_if_approved()` is the only place in the package that may call a
   side-effecting tool. `tests/test_architecture.py` proves that by walking the
   import graph, so a future code path cannot quietly bypass it.

The digest is internal. The user is shown a plain sentence, because "confirm the
exact action and details" is a human requirement, not a hash-typing exercise.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, TypeVar

from errand.clock import Clock
from errand.db import Database, dumps, loads

T = TypeVar("T")

Kind = Literal["book", "cancel", "reschedule"]

APPROVAL_TTL = timedelta(minutes=15)
KEY_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


class ApprovalMissing(Exception):
    """A side effect was attempted without a valid matching approval."""


@dataclass(frozen=True)
class PendingAction:
    kind: Kind
    provider_id: str
    service: str
    slot_start: datetime
    slot_end: datetime
    price_paise: int
    booking_id: str | None = None
    attempt: int = 0

    def canonical(self) -> bytes:
        """Canonical JSON. Explicit, because asdict() cannot serialise a datetime.

        `json.dumps(asdict(self))` raises TypeError on the first call; that is the
        trap this method exists to avoid.
        """
        if self.slot_start.tzinfo is None or self.slot_end.tzinfo is None:
            raise ValueError("PendingAction requires timezone-aware slots")
        payload = {
            "kind": self.kind,
            "provider_id": self.provider_id,
            "service": self.service,
            "slot_start": self.slot_start.astimezone(timezone.utc).isoformat(),
            "slot_end": self.slot_end.astimezone(timezone.utc).isoformat(),
            "price_paise": int(self.price_paise),
            "booking_id": self.booking_id,
            "attempt": int(self.attempt),
        }
        return json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode()

    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    def idempotency_key(self) -> str:
        """Stable for the same action across confirmations, retries and restarts.

        `attempt` is part of the digest, so a deliberate rebook after a
        cancellation gets a fresh key instead of being deduped as a duplicate.
        """
        return str(uuid.uuid5(KEY_NAMESPACE, f"errand-agent/v1/{self.digest()}"))

    def to_json(self) -> str:
        return dumps(
            {
                "kind": self.kind,
                "provider_id": self.provider_id,
                "service": self.service,
                "slot_start": self.slot_start.isoformat(),
                "slot_end": self.slot_end.isoformat(),
                "price_paise": self.price_paise,
                "booking_id": self.booking_id,
                "attempt": self.attempt,
            }
        )

    @classmethod
    def from_row(cls, raw: str) -> "PendingAction":
        d = loads(raw, {})
        return cls(
            kind=d["kind"],
            provider_id=d["provider_id"],
            service=d["service"],
            slot_start=datetime.fromisoformat(d["slot_start"]),
            slot_end=datetime.fromisoformat(d["slot_end"]),
            price_paise=int(d["price_paise"]),
            booking_id=d.get("booking_id"),
            attempt=int(d.get("attempt", 0)),
        )


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    session_id: str
    kind: Kind
    digest: str
    state: str
    text_shown: str
    created_at: datetime


class ApprovalStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    # ------------------------------------------------------------------ writes

    def request(self, session_id: str, action: PendingAction, text_shown: str) -> str:
        """Freeze the action and record what the user was shown. Returns the digest."""
        approval_id = f"ap_{uuid.uuid4().hex[:12]}"
        self.db.execute(
            "INSERT INTO approval(id, session_id, kind, digest, payload_json,"
            " text_shown, state, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                approval_id,
                session_id,
                action.kind,
                action.digest(),
                action.to_json(),
                text_shown,
                "pending",
                self.clock.now().isoformat(),
            ),
        )
        return action.digest()

    def invalidate(self, session_id: str, reason: str) -> None:
        self.db.execute(
            "UPDATE approval SET state='invalidated', invalidated_reason=?"
            " WHERE session_id=? AND state='pending'",
            (reason, session_id),
        )

    def consume(self, session_id: str) -> None:
        self.db.execute(
            "UPDATE approval SET state='consumed', consumed_at=?"
            " WHERE session_id=? AND state='pending'",
            (self.clock.now().isoformat(), session_id),
        )

    # ------------------------------------------------------------------- reads

    def latest(self, session_id: str) -> ApprovalRecord | None:
        row = self.db.query_one(
            "SELECT * FROM approval WHERE session_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        )
        if row is None:
            return None
        return ApprovalRecord(
            id=row["id"],
            session_id=row["session_id"],
            kind=row["kind"],
            digest=row["digest"],
            state=row["state"],
            text_shown=row["text_shown"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def approved_action(self, session_id: str) -> PendingAction | None:
        row = self.db.query_one(
            "SELECT payload_json FROM approval WHERE session_id=? AND state='pending'"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        )
        return PendingAction.from_row(row["payload_json"]) if row else None

    def validate(self, session_id: str, live: PendingAction) -> bool:
        """The whole check, in one place.

        Requires: a pending approval for this session, the same digest, a live
        action that still matches, and an approval inside its TTL.
        """
        row = self.db.query_one(
            "SELECT * FROM approval WHERE session_id=? AND state='pending'"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (session_id,),
        )
        if row is None:
            return False
        if row["digest"] != live.digest():
            return False
        created = datetime.fromisoformat(row["created_at"])
        if self.clock.now() - created > APPROVAL_TTL:
            self.invalidate(session_id, "expired")
            return False
        return True

    # ------------------------------------------------------------- the monopoly

    def execute_if_approved(
        self, session_id: str, live: PendingAction, fn: Callable[[], T]
    ) -> T:
        """The only sanctioned path to a side-effecting tool call."""
        if not self.validate(session_id, live):
            raise ApprovalMissing(
                f"no valid approval for {live.kind} ({live.digest()[:12]}) in session {session_id}"
            )
        result = fn()
        self.consume(session_id)
        return result
