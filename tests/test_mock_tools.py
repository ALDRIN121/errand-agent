"""Phase 1 gate: the mock environment is deterministic and fails the way it claims.

These tests exist because every later guarantee (approval, idempotency, recovery)
is measured against this environment. A mock that is too kind silently removes the
code paths the whole design is about.
"""
from __future__ import annotations

import pytest

from errand.clock import FakeClock
from errand.tools.mock import SCENARIOS, MockToolServer
from errand.types import ToolError


def make(scenario: str = "happy", seed: int = 7) -> tuple[MockToolServer, FakeClock]:
    clock = FakeClock()
    return MockToolServer(clock=clock, seed=seed, scenario=scenario), clock


def first_slot(server: MockToolServer, provider_id: str):
    return server.get_provider(provider_id).slots[0]


# ------------------------------------------------------------------ determinism


def test_same_seed_same_call_log():
    a, _ = make(seed=3)
    b, _ = make(seed=3)
    for s in (a, b):
        p = s.search_providers("plumber", "Indiranagar")[0]
        s.hold_slot(p.id, p.slots[0])
    assert [c.name for c in a.calls] == [c.name for c in b.calls]
    assert a.calls[-1].result == b.calls[-1].result


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError):
        MockToolServer(clock=FakeClock(), scenario="nope")


def test_all_declared_scenarios_construct():
    for name in SCENARIOS:
        MockToolServer(clock=FakeClock(), scenario=name)


# ------------------------------------------------------------ probability split


def test_book_outcome_split_over_1000_seeded_draws():
    """70 / 15 / 15, with half of the timeouts having actually booked."""
    successes = taken = timeouts = booked_on_timeout = 0
    for i in range(1000):
        server, _ = make(seed=i)
        p = server.search_providers("plumber", "Indiranagar")[0]
        hold = server.hold_slot(p.id, p.slots[0])
        key = f"key-{i}"
        try:
            server.book(hold.hold_id, key)
            successes += 1
        except ToolError as exc:
            if exc.error_class == "slot_taken":
                taken += 1
            elif exc.error_class == "timeout":
                timeouts += 1
                if server.get_booking_status(idempotency_key=key)["status"] == "booked":
                    booked_on_timeout += 1
            else:  # pragma: no cover - would be a real defect
                raise

    assert 0.66 < successes / 1000 < 0.74, successes
    assert 0.12 < taken / 1000 < 0.18, taken
    assert 0.12 < timeouts / 1000 < 0.18, timeouts
    # half of timeouts actually booked
    assert 0.35 < booked_on_timeout / timeouts < 0.65, (booked_on_timeout, timeouts)


# ------------------------------------------------------------------- book order


def test_book_dedupes_on_repeated_key():
    server, _ = make()
    p = server.search_providers("plumber", "Indiranagar")[0]
    hold = server.hold_slot(p.id, p.slots[0])
    key = "idem-1"

    try:
        first = server.book(hold.hold_id, key)
    except ToolError as exc:
        pytest.skip(f"seed drew {exc.error_class}")

    second = server.book(hold.hold_id, key)
    assert first.booking_id == second.booking_id
    assert len({b.booking_id for b in server._by_key.values() if b.idempotency_key == key}) == 1


def test_book_rejects_expired_hold_before_dedupe():
    """Expiry must be checked first: a stale hold must never return a booking."""
    server, clock = make()
    p = server.search_providers("plumber", "Indiranagar")[0]
    hold = server.hold_slot(p.id, p.slots[0])
    clock.advance(601)  # past the 10-minute hold window

    with pytest.raises(ToolError) as exc:
        server.book(hold.hold_id, "some-key")
    assert exc.value.error_class == "hold_expired"


def test_book_unknown_hold_is_validation_error():
    server, _ = make()
    with pytest.raises(ToolError) as exc:
        server.book("hold_missing", "k")
    assert exc.value.error_class == "validation"


# --------------------------------------------------------------------- cancel


def test_cancel_fails_transient_then_succeeds():
    server, _ = make()
    p = server.search_providers("plumber", "Indiranagar")[0]
    hold = server.hold_slot(p.id, p.slots[0])
    try:
        booking = server.book(hold.hold_id, "cancel-key")
    except ToolError:
        pytest.skip("draw did not book")

    with pytest.raises(ToolError) as exc:
        server.cancel(booking.booking_id, "cancel-key")
    assert exc.value.error_class == "transient"

    # the retry, same key
    result = server.cancel(booking.booking_id, "cancel-key")
    assert result.status == "cancelled"


# ----------------------------------------------------------------- reschedule


def test_reschedule_conflicts_on_configured_hour():
    server, _ = make(scenario="reschedule_conflict")
    p = server.search_providers("plumber", "Indiranagar")[0]
    hold = server.hold_slot(p.id, p.slots[0])
    booking = server._make_booking(hold, "rs-key")

    conflict_slot = next(s for s in p.slots if s.start.hour == 9)
    with pytest.raises(ToolError) as exc:
        server.reschedule(booking.booking_id, conflict_slot, "rs-key")
    assert exc.value.error_class == "slot_conflict"


def test_reschedule_succeeds_off_conflict_hour():
    server, _ = make(scenario="reschedule_ok")
    p = server.search_providers("plumber", "Indiranagar")[0]
    hold = server.hold_slot(p.id, p.slots[0])
    booking = server._make_booking(hold, "rs-key-2")

    free_slot = next(s for s in p.slots if s.start.hour == 11)
    out = server.reschedule(booking.booking_id, free_slot, "rs-key-2")
    assert out.slot.start.hour == 11


# ------------------------------------------------------------------- contract


def test_search_returns_between_3_and_8_with_required_fields():
    server, _ = make()
    results = server.search_providers("plumber", "Indiranagar")
    assert 3 <= len(results) <= 8
    for p in results:
        assert p.id and p.price_paise > 0 and 0 < p.rating <= 5 and p.slots


def test_get_provider_carries_untrusted_injection():
    server, _ = make()
    reviewed = [pid for pid in server._providers if server._providers[pid].seed_index % 3 in (0, 1)]
    assert reviewed
    for pid in reviewed[:2]:
        reviews = server.get_reviews(pid)
        assert any("IGNORE PREVIOUS" in r.text or "SYSTEM:" in r.text for r in reviews)
        assert all(r.trust == "untrusted" for r in reviews)


def test_hold_quote_can_differ_under_price_shift():
    plain, _ = make()
    shifted, _ = make(scenario="price_shift")
    p = plain.search_providers("plumber", "Indiranagar")[0]
    h1 = plain.hold_slot(p.id, p.slots[0])
    h2 = shifted.hold_slot(p.id, p.slots[0])
    assert h2.quote_paise == h1.quote_paise + 40000


def test_rehold_same_slot_returns_same_hold_and_quote():
    server, _ = make()
    p = server.search_providers("plumber", "Indiranagar")[0]
    a = server.hold_slot(p.id, p.slots[0])
    b = server.hold_slot(p.id, p.slots[0])
    assert a.hold_id == b.hold_id and a.quote_paise == b.quote_paise


def test_get_booking_status_requires_a_key():
    server, _ = make()
    with pytest.raises(ToolError) as exc:
        server.get_booking_status()
    assert exc.value.error_class == "validation"


def test_get_booking_status_reports_not_booked_for_unused_key():
    server, _ = make()
    out = server.get_booking_status(idempotency_key="never-used")
    assert out == {"status": "not_booked", "booking_id": None}


def test_list_bookings_empty_then_populated():
    server, _ = make()
    assert server.list_bookings("u_local") == []
    p = server.search_providers("plumber", "Indiranagar")[0]
    hold = server.hold_slot(p.id, p.slots[0])
    server._make_booking(hold, "list-key")
    rows = server.list_bookings("u_local")
    assert len(rows) == 1 and rows[0].status == "confirmed"


def test_emit_provider_cancel_marks_status_and_queues_event():
    server, _ = make()
    p = server.search_providers("plumber", "Indiranagar")[0]
    hold = server.hold_slot(p.id, p.slots[0])
    booking = server._make_booking(hold, "cancel-evt")

    server.emit_provider_cancel(booking.booking_id)
    assert booking.status == "cancelled_by_provider"
    assert server.events == [{"kind": "provider_cancel", "booking_id": booking.booking_id}]


def test_every_call_is_recorded():
    server, _ = make()
    server.search_providers("plumber", "Indiranagar")
    server.list_bookings("u_local")
    assert [c.name for c in server.calls] == ["search_providers", "list_bookings"]


def test_all_pairs_satisfy_contract():
    """Guards Bug 1: every (service, area) pair must return 3-8 providers.

    The first fixture generated providers with i % 3 / (i // 3) % 3, which reached
    only six of the nine pairs and left two of those with a single provider.
    """
    server, _ = make()
    pairs = {(p.service, p.area) for p in server._providers.values()}
    assert len(pairs) == 9, f"fixture covers {len(pairs)} pairs: {sorted(pairs)}"
    for service, area in sorted(pairs):
        n = len(server.search_providers(service, area))
        assert 3 <= n <= 8, f"{service}/{area} returned {n}"
