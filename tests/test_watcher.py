"""Phase 5 gate: the unattended path.

The properties that matter here are the ones an unattended agent gets wrong: firing
twice, firing the side effect before the user agrees, and silently using a model
where the design says rules.
"""
from __future__ import annotations

import pytest

from errand.approval import ApprovalStore
from errand.clock import FakeClock
from errand.context import ContextAssembler
from errand.db import Database
from errand.llm import ScriptedLLM
from errand.orchestrator import Orchestrator
from errand.watcher import Watcher
from evals.demo_data import ANCHOR, build_world


@pytest.fixture
def rig():
    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, user_id="u_aldrin")
    orch = Orchestrator(
        clock=clock, server=world.server, db=world.db, memory=world.memory,
        history=world.history, approvals=ApprovalStore(world.db, clock),
        llm=ScriptedLLM([]), context=ContextAssembler(), user_id="u_aldrin",
    )
    watcher = Watcher(server=world.server, orchestrator=orch, db=world.db, clock=clock)
    yield world, orch, watcher, clock
    world.close()


def test_provider_cancel_queues_exactly_one_message(rig):
    world, orch, watcher, _ = rig
    world.server.emit_provider_cancel("bk_1001")
    queued = watcher.tick()
    assert len(queued) == 1
    assert len(watcher.pending()) == 1


def test_a_second_tick_adds_nothing(rig):
    """A proactive path that re-fires is a path that spams the user."""
    world, orch, watcher, _ = rig
    world.server.emit_provider_cancel("bk_1001")
    watcher.tick()
    assert watcher.tick() == []
    assert len(watcher.pending()) == 1


def test_the_message_names_the_provider_the_slot_and_the_price(rig):
    """The user has to be able to decide from the message alone."""
    world, orch, watcher, _ = rig
    world.server.emit_provider_cancel("bk_1001")
    body = watcher.tick()[0]
    assert "prov_06" in body, "the message must name who cancelled"
    assert "prov_04" in body, "the message must name the replacement"
    assert "Rs" in body, "the message must name the price"
    assert any(d in body for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))


def test_no_booking_happens_before_an_affirmative_turn(rig):
    """The offer alone must not book: approval is still required."""
    world, orch, watcher, _ = rig
    world.server.emit_provider_cancel("bk_1001")
    watcher.tick()
    assert world.server.calls_named("book") == []
    assert orch.session.state.value == "AWAITING_APPROVAL"


def test_the_recovery_path_makes_no_model_calls(rig):
    """G2: rules only. A model here would add cost and nondeterminism to the one
    path with no human watching, and the decision is a sort."""
    world, orch, watcher, _ = rig
    world.server.emit_provider_cancel("bk_1001")
    watcher.tick()
    assert orch.llm.prompts == [], "the proactive path must not call the model"
    assert orch.llm.consumed == []


def test_affirming_the_offer_books_the_replacement(rig):
    world, orch, watcher, _ = rig
    world.server.emit_provider_cancel("bk_1001")
    watcher.tick()
    result = orch.handle_turn("yes")
    assert result.outcome == "booked"
    assert len(world.server.calls_named("book")) == 1


def test_declining_records_the_booking_and_ends_cancelled(rig):
    """Scenario 37: declining must leave the original cancelled and book nothing."""
    world, orch, watcher, _ = rig
    world.server.emit_provider_cancel("bk_1001")
    watcher.tick()
    result = orch.handle_turn("no thanks")
    world.server.calls_named("book")
    assert result.outcome in ("abandoned_by_user", "cancelled")
    assert world.server.calls_named("book") == []


def test_an_event_raised_while_down_still_fires_at_startup(rig):
    """tick() runs once at start-up, so a cancellation during downtime is not lost."""
    world, orch, watcher, _ = rig
    world.server.emit_provider_cancel("bk_1002")
    # nothing consumed it yet; the first tick is the "start-up" tick
    assert len(watcher.tick()) == 1


def test_delivery_marks_rows_delivered(rig):
    world, orch, watcher, _ = rig
    world.server.emit_provider_cancel("bk_1001")
    watcher.tick()
    bodies = watcher.drain_for_display()
    assert len(bodies) == 1
    assert watcher.pending() == []


def test_unknown_event_kind_is_rejected_not_swallowed(rig):
    world, orch, watcher, _ = rig
    world.server.events.append({"kind": "moon_phase", "booking_id": "bk_1001"})
    assert watcher.tick() == []  # the loop survives
    assert watcher.pending() == []  # nothing was queued


def test_an_event_for_an_unknown_booking_queues_nothing(rig):
    world, orch, watcher, _ = rig
    world.server.events.append({"kind": "provider_cancel", "booking_id": "bk_9999"})
    assert watcher.tick() == []


def test_replacement_ranking_is_rating_then_price_then_id(rig):
    """G2's ordering is asserted, because three plausible orderings give three demos."""
    world, orch, watcher, _ = rig
    world.server.emit_provider_cancel("bk_1001")  # plumber in Koramangala
    watcher.tick()
    replacement = orch.pending.provider_id
    candidates = [
        p for p in world.server.search_providers("plumber", "Koramangala")
        if p.id != "prov_06"
    ]
    expected = sorted(candidates, key=lambda p: (-p.rating, p.price_paise, p.id))[0]
    assert replacement == expected.id


def test_outcomes_vocabulary_matches_the_scenario_vocabulary():
    """Two hand-written token lists is how `cancelled` went missing from the
    renderer while the scenarios still expected it."""
    from errand.outcomes import OUTCOMES
    from tests.test_scenarios import OUTCOME_TOKENS

    assert set(OUTCOMES) == OUTCOME_TOKENS


def test_every_token_renders_without_raising():
    from errand.outcomes import OUTCOMES, render_outcome

    for token in OUTCOMES:
        sentence = render_outcome(token)
        assert sentence and sentence[0].isupper(), token
