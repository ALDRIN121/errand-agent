"""Which actions are legal, and in which state.

This module is the single source of legality. Nothing else may decide whether a
tool call is permitted: a second opinion elsewhere is a second place for the rule
to drift.

The important property is not "the model is asked nicely to wait for approval".
It is that `book`, `cancel` and `reschedule` are absent from the legal set in every
state except EXECUTING, and EXECUTING is unreachable without a valid approval.
"""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    NEW = "NEW"
    CLARIFYING = "CLARIFYING"
    DISCOVERING = "DISCOVERING"
    SEARCHING = "SEARCHING"
    SELECTING = "SELECTING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    PAUSED = "PAUSED"
    TERMINAL = "TERMINAL"


SIDE_EFFECTING = frozenset({"book", "cancel", "reschedule"})

CONTRACT_TOOLS = frozenset(
    {
        "search_providers",
        "get_provider",
        "hold_slot",
        "book",
        "get_booking_status",
        "cancel",
        "reschedule",
        "list_bookings",
    }
)

LEGAL: dict[State, frozenset[str]] = {
    State.NEW: frozenset(),
    State.CLARIFYING: frozenset(),
    State.DISCOVERING: frozenset({"list_bookings"}),
    State.SEARCHING: frozenset({"search_providers"}),
    State.SELECTING: frozenset({"get_provider", "hold_slot"}),
    State.AWAITING_APPROVAL: frozenset(),
    # EXECUTING is narrowed at runtime to the one tool matching approval.kind
    State.EXECUTING: SIDE_EFFECTING,
    State.VERIFYING: frozenset({"get_booking_status"}),
    State.PAUSED: frozenset({"hold_slot"}),
    State.TERMINAL: frozenset(),
}

TRANSITIONS: dict[State, frozenset[State]] = {
    State.NEW: frozenset({State.CLARIFYING, State.DISCOVERING, State.SEARCHING, State.TERMINAL}),
    State.CLARIFYING: frozenset({State.SEARCHING, State.DISCOVERING, State.TERMINAL}),
    State.DISCOVERING: frozenset(
        {State.AWAITING_APPROVAL, State.SEARCHING, State.CLARIFYING, State.TERMINAL}
    ),
    State.SEARCHING: frozenset({State.SELECTING, State.TERMINAL}),
    State.SELECTING: frozenset({State.AWAITING_APPROVAL, State.SEARCHING, State.TERMINAL}),
    State.AWAITING_APPROVAL: frozenset(
        {State.EXECUTING, State.SELECTING, State.DISCOVERING, State.PAUSED, State.TERMINAL}
    ),
    State.EXECUTING: frozenset({State.VERIFYING, State.TERMINAL}),
    State.VERIFYING: frozenset({State.TERMINAL, State.EXECUTING}),
    State.PAUSED: frozenset({State.AWAITING_APPROVAL, State.SELECTING, State.TERMINAL}),
    State.TERMINAL: frozenset(),
}


class PolicyViolation(Exception):
    """An action the current state does not permit. Never silently downgraded."""


def legal_tools(state: State, approval_kind: str | None = None) -> frozenset[str]:
    """The tool set the model may be offered in this state.

    EXECUTING collapses to exactly the approved verb. Passing no kind yields the
    empty set rather than all three, so a caller who forgets the approval gets a
    refusal instead of a carte blanche.
    """
    if state is State.EXECUTING:
        if approval_kind in SIDE_EFFECTING:
            return frozenset({approval_kind})
        return frozenset()
    return LEGAL[state]


def assert_tool_allowed(state: State, tool: str, approval_kind: str | None = None) -> None:
    allowed = legal_tools(state, approval_kind)
    if tool not in allowed:
        raise PolicyViolation(
            f"tool {tool!r} is not legal in state {state.value!r} "
            f"(allowed: {sorted(allowed) or 'none'})"
        )


def transition_allowed(src: State, dst: State) -> bool:
    return dst in TRANSITIONS[src]


def missing_slots(slots: dict) -> list[str]:
    """Required slots not yet established. Order is stable for deterministic tests."""
    required = ("service", "area", "window_start", "window_end")
    return [f for f in required if slots.get(f) in (None, "", [])]
