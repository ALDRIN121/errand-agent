"""Phase 2 gate, part 2: the approval digest and the consume-once rule."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from errand.approval import (
    ApprovalMissing,
    ApprovalStore,
    PendingAction,
)
from errand.clock import IST, FakeClock
from errand.db import Database


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / "t.db")
    clock = FakeClock()
    yield ApprovalStore(db, clock), db, clock
    db.close()


def action(**over) -> PendingAction:
    base = dict(
        kind="book",
        provider_id="prov_01",
        service="plumber",
        slot_start=datetime(2026, 11, 24, 9, 0, tzinfo=IST),
        slot_end=datetime(2026, 11, 24, 10, 0, tzinfo=IST),
        price_paise=145000,
        booking_id=None,
        attempt=0,
    )
    base.update(over)
    return PendingAction(**base)


# ------------------------------------------------------------------- the digest


def test_digest_is_stable_for_identical_content():
    assert action().digest() == action().digest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", "cancel"),
        ("provider_id", "prov_02"),
        ("service", "electrician"),
        ("slot_start", datetime(2026, 11, 24, 10, 0, tzinfo=IST)),
        ("price_paise", 145001),
        ("booking_id", "bk_0001"),
        ("attempt", 1),
    ],
)
def test_digest_changes_on_every_material_field(field, value):
    assert action().digest() != action(**{field: value}).digest()


def test_digest_survives_timezone_representation():
    """The same instant expressed differently must hash the same."""
    utc = datetime(2026, 11, 24, 3, 30, tzinfo=UTC)
    ist = utc.astimezone(IST)
    a = action(slot_start=utc, slot_end=utc + timedelta(hours=1))
    b = action(slot_start=ist, slot_end=ist + timedelta(hours=1))
    assert a.digest() == b.digest()


def test_naive_datetime_is_rejected_not_silently_coerced():
    naive = datetime(2026, 11, 24, 9, 0)
    with pytest.raises(ValueError):
        action(slot_start=naive, slot_end=naive).digest()


def test_canonical_does_not_use_asdict():
    """Guards the documented trap: json.dumps(asdict(...)) raises TypeError here."""
    import json
    from dataclasses import asdict

    with pytest.raises(TypeError):
        json.dumps(asdict(action()), sort_keys=True)
    # while the real implementation works
    assert action().canonical()


def test_idempotency_key_stable_then_distinct_after_attempt_bump():
    a = action()
    assert a.idempotency_key() == action().idempotency_key()
    assert a.idempotency_key() != action(attempt=1).idempotency_key()


def test_cancel_and_book_on_same_slot_have_different_digests():
    """`kind` is in the payload, so a cancel approval cannot authorise a book."""
    assert action(kind="book").digest() != action(kind="cancel").digest()


# ------------------------------------------------------- validate and consume


def test_validate_requires_a_pending_approval(store):
    s, db, clock = store
    assert s.validate("sess", action()) is False


def test_request_then_validate_succeeds(store):
    s, db, clock = store
    s.request("sess", action(), "Book prov_01 for 09:00 at Rs 1,450?")
    assert s.validate("sess", action()) is True


def test_validate_rejects_a_material_change(store):
    s, db, clock = store
    s.request("sess", action(), "shown")
    assert s.validate("sess", action(price_paise=149000)) is False
    assert s.validate("sess", action(slot_start=datetime(2026, 11, 24, 10, 0, tzinfo=IST),
                                    slot_end=datetime(2026, 11, 24, 11, 0, tzinfo=IST))) is False


def test_validate_rejects_an_expired_approval(store):
    s, db, clock = store
    s.request("sess", action(), "shown")
    clock.advance(16 * 60)  # beyond the 15-minute TTL
    assert s.validate("sess", action()) is False


def test_consumed_approval_cannot_be_replayed(store):
    s, db, clock = store
    s.request("sess", action(), "shown")
    assert s.validate("sess", action()) is True
    s.consume("sess")
    assert s.validate("sess", action()) is False


def test_invalidate_clears_pending(store):
    s, db, clock = store
    s.request("sess", action(), "shown")
    s.invalidate("sess", "price changed")
    assert s.validate("sess", action()) is False


# --------------------------------------------------------------- the monopoly


def test_execute_if_approved_runs_the_callable_once(store):
    s, db, clock = store
    s.request("sess", action(), "shown")
    calls = []
    out = s.execute_if_approved("sess", action(), lambda: calls.append(1) or "ok")
    assert out == "ok" and calls == [1]
    # consumed, so a second attempt is refused
    with pytest.raises(ApprovalMissing):
        s.execute_if_approved("sess", action(), lambda: calls.append(2))


def test_execute_if_approved_refuses_without_approval(store):
    s, db, clock = store
    with pytest.raises(ApprovalMissing):
        s.execute_if_approved("sess", action(), lambda: "nope")


def test_execute_if_approved_refuses_a_different_action(store):
    s, db, clock = store
    s.request("sess", action(), "shown for book")
    with pytest.raises(ApprovalMissing):
        s.execute_if_approved("sess", action(kind="cancel"), lambda: "nope")


def test_approval_payload_round_trips(store):
    s, db, clock = store
    original = action(booking_id="bk_9", attempt=2)
    s.request("sess", original, "shown")
    assert s.approved_action("sess") == original
