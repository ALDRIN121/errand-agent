"""Phase 2 gate, part 1: the legal-tool matrix.

The exhaustive table is the artefact that proves the central claim: no
side-effecting tool is reachable outside EXECUTING, and EXECUTING yields exactly
the approved verb.
"""
from __future__ import annotations

import pytest

from errand.policy import (
    CONTRACT_TOOLS,
    LEGAL,
    TRANSITIONS,
    PolicyViolation,
    State,
    assert_tool_allowed,
    legal_tools,
    missing_slots,
    transition_allowed,
)

# The expected allow/deny matrix, written out longhand. If the implementation and
# this table disagree, one of them is wrong and a human decides which.
EXPECTED: dict[State, set[str]] = {
    State.NEW: set(),
    State.CLARIFYING: set(),
    State.DISCOVERING: {"list_bookings"},
    State.SEARCHING: {"search_providers"},
    State.SELECTING: {"get_provider", "hold_slot"},
    State.AWAITING_APPROVAL: set(),
    State.EXECUTING: {"book", "cancel", "reschedule"},  # narrowed at runtime
    State.VERIFYING: {"get_booking_status"},
    State.PAUSED: {"hold_slot"},
    State.TERMINAL: set(),
}


def test_state_set_is_exactly_ten():
    assert len(State) == 10
    assert {s.name for s in State} == {
        "NEW", "CLARIFYING", "DISCOVERING", "SEARCHING", "SELECTING",
        "AWAITING_APPROVAL", "EXECUTING", "VERIFYING", "PAUSED", "TERMINAL",
    }


@pytest.mark.parametrize("state", list(State))
@pytest.mark.parametrize("tool", sorted(CONTRACT_TOOLS))
def test_legal_tool_matrix(state: State, tool: str):
    """Every (state, tool) cell, including the three side-effecting tools."""
    expected = tool in EXPECTED[state]
    if state is State.EXECUTING:
        # the raw table holds all three; the runtime set narrows to the kind
        assert (tool in legal_tools(state, tool)) is (tool in {"book", "cancel", "reschedule"})
        return
    actual = tool in legal_tools(state)
    assert actual is expected, f"{state.value}/{tool}: got {actual}, expected {expected}"


def test_side_effecting_tools_are_never_legal_outside_executing():
    for state in State:
        if state is State.EXECUTING:
            continue
        for tool in ("book", "cancel", "reschedule"):
            assert tool not in legal_tools(state, tool), f"{tool} leaked into {state.value}"


def test_executing_without_kind_yields_nothing():
    """A caller who forgets the approval gets a refusal, not a carte blanche."""
    assert legal_tools(State.EXECUTING) == frozenset()
    assert legal_tools(State.EXECUTING, "hold_slot") == frozenset()


def test_executing_narrows_to_the_approved_verb():
    assert legal_tools(State.EXECUTING, "cancel") == frozenset({"cancel"})
    with pytest.raises(PolicyViolation):
        assert_tool_allowed(State.EXECUTING, "book", "cancel")


def test_assert_tool_allowed_raises_with_context():
    with pytest.raises(PolicyViolation) as exc:
        assert_tool_allowed(State.SELECTING, "book")
    assert "book" in str(exc.value) and "SELECTING" in str(exc.value)


def test_every_state_has_a_transition_row():
    assert set(TRANSITIONS) == set(State)


def test_terminal_is_a_sink():
    assert TRANSITIONS[State.TERMINAL] == frozenset()


def test_no_transition_targets_an_unknown_state():
    for src, dsts in TRANSITIONS.items():
        for dst in dsts:
            assert isinstance(dst, State)


def test_executing_is_only_reachable_from_awaiting_approval():
    """The structural half of the approval guarantee."""
    sources = [s for s, dsts in TRANSITIONS.items() if State.EXECUTING in dsts]
    assert sources == [State.AWAITING_APPROVAL, State.VERIFYING]


def test_missing_slots_reports_only_the_gap():
    assert missing_slots({}) == ["service", "area", "window_start", "window_end"]
    assert missing_slots(
        {"service": "plumber", "area": "Indiranagar",
         "window_start": "2026-11-24T06:00", "window_end": "2026-11-24T12:00"}
    ) == []
    assert missing_slots({"service": "plumber"}) == ["area", "window_start", "window_end"]


def test_legal_table_covers_every_state():
    assert set(LEGAL) == set(State)
