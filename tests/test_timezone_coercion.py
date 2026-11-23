"""A model writes timestamps the way a person does, and that must still work.

Reported live: "Move my AC servicing to Saturday" → "yes, 3pm" returned
`ValueError: PendingAction requires timezone-aware slots`. The model had sent
`2026-11-28T15:00:00` with no offset — which is exactly what any reasonable model
writes — and the naive datetime travelled all the way into the approval store before
being rejected, surfacing as a raw exception rather than a booked slot.

The fix reads an offset-less timestamp as local time. This file pins that, and pins
that an explicit offset is *not* overridden.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from errand.clock import IST
from errand.policy import State
from errand.tools.gateway import Gateway, _coerce, _parse_dt
from errand.tools.mock import MockToolServer
from evals.demo_data import ANCHOR


def _server():
    from errand.clock import FakeClock

    return MockToolServer(FakeClock(ANCHOR.replace(hour=8)), seed=7)


# ------------------------------------------------------------------ parsing


def test_a_naive_iso_string_is_read_as_local_time():
    got = _parse_dt("2026-11-28T15:00:00")
    assert got.tzinfo is not None
    assert got.utcoffset() == IST.utcoffset(None)
    assert (got.hour, got.minute) == (15, 0), "the wall-clock reading must not shift"


def test_an_explicit_offset_is_preserved():
    got = _parse_dt("2026-11-28T15:00:00+00:00")
    assert got.utcoffset() == timezone.utc.utcoffset(None)


def test_a_naive_datetime_object_is_read_as_local_time():
    got = _parse_dt(datetime(2026, 11, 28, 15, 0))
    assert got.tzinfo is not None and got.hour == 15


def test_an_aware_datetime_object_is_left_alone():
    given = datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc)
    assert _parse_dt(given) is given


# ---------------------------------------------------------------- coercion


def test_coercing_a_naive_slot_yields_an_aware_one():
    out = _coerce("hold_slot", {
        "provider_id": "prov_31",
        "slot": {"start": "2026-11-28T15:00:00", "end": "2026-11-28T16:00:00"},
    })
    assert out["slot"].start.tzinfo is not None
    assert out["slot"].end.tzinfo is not None


def test_a_held_naive_slot_survives_into_an_approval():
    """The reported failure: the slot must be freezable into a PendingAction."""
    server = _server()
    result = Gateway(server=server, user_id="u_aldrin").call(
        State.SELECTING, "hold_slot",
        {"provider_id": "prov_31",
         "slot": {"start": "2026-11-28T15:00:00", "end": "2026-11-28T16:00:00"}},
    )
    assert result.ok, result.error
    hold = result.value
    assert hold.slot.start.tzinfo is not None

    from errand.approval import PendingAction

    pending = PendingAction(
        kind="reschedule", provider_id=hold.provider_id, service="ac_service",
        slot_start=hold.slot.start, slot_end=hold.slot.end,
        price_paise=0, booking_id="bk_1003", attempt=0,
    )
    # `digest()` is what raised in production; it must not raise now.
    assert pending.digest()


def test_a_malformed_timestamp_is_still_a_structured_refusal():
    """Adding timezone inference must not turn a bad value into an exception."""
    result = Gateway(server=_server()).call(
        State.SELECTING, "hold_slot",
        {"provider_id": "prov_31", "slot": {"start": "next saturday-ish"}},
    )
    assert not result.ok
    assert result.error.error_class == "validation"
