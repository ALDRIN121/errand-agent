"""Admission: turning a model proposal into a first move, or into a question.

NEW and CLARIFYING dispatch nothing, so a proposal that arrives there is *intent*.
This module reads the arguments the model supplied and decides what that intent is
allowed to become: a search, a booking reference lookup, a research detour, or a
question naming the choices. It mutates no state directly beyond the slots the intent
established — the transitions and the tool call stay in the orchestrator, which owns
them.

The distinction that matters: admission never executes a side effect. A `book` here
opens an approval, and the user still has to say yes.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from errand.areas import known_areas
from errand.llm import Action
from errand.policy import State
from errand.session import TurnResult

if TYPE_CHECKING:                      # pragma: no cover - avoids an import cycle
    from errand.orchestrator import Orchestrator


def admit(orch: Orchestrator, action: Action) -> TurnResult:
    """Leave NEW/CLARIFYING by reading the slots out of the model's arguments.

    A proposal that carries the missing slots is the model's answer to the
    question, in structured form. If it does not carry them, the turn becomes a
    CLARIFYING turn and the question is the model's own message.
    """
    name = action.tool or ""
    args = action.args or {}

    if name in ("book", "cancel", "reschedule"):
        # Opening an approval is not executing: _open_approval raises the ask and
        # the user still has to say yes. `book` with no hold has nothing to ask
        # about, so that one is refused.
        return orch._open_approval(action)

    if name == "web_search":
        if not str(args.get("query") or "").strip():
            msg = "What should I look up?"
            orch._clarify(msg)
            return orch._result(name, msg)
        return orch._open_research(action)

    if name == "list_bookings":
        if orch.session.state is not State.DISCOVERING:
            orch._transition(State.DISCOVERING)
        return orch._dispatch(action)

    if name == "search_providers":
        service = args.get("service")
        area = args.get("area")
        window = args.get("date_range")
        # area was already resolved and validated before admission.
        if orch.session.state is State.SELECTING and service and area:
            # "same electrician as last time" resolves a provider, then searches
            # their free slots: the target slots are known, so SELECTING -> SEARCHING
            # is the right hop before asking the tool.
            orch.session.slots.setdefault("service", service)
            orch.session.slots.setdefault("area", area)
            orch._transition(State.SEARCHING)
            return orch._dispatch(action)
        # date_range is optional in the contract (the mock defaults to the next 7
        # days), so a proposal with service+area alone is a complete search. Demanding
        # all three parked a real model in CLARIFYING indefinitely.
        if service and area and window is not None:

            orch.session.slots.update(
                service=service, area=area,
                window_start=window.get("start") if isinstance(window, dict) else window,
                window_end=window.get("end") if isinstance(window, dict) else window,
            )
            orch.session.slots.pop("reason", None)
            orch._transition(State.SEARCHING)
            return orch._dispatch(action)
        if service and area:
            # no window given: search the default range
            orch.session.slots.update(service=service, area=area)
            orch.session.slots.pop("reason", None)
            orch._transition(State.SEARCHING)
            return orch._dispatch(action)
        # incomplete proposal: treat as a clarifying turn, and make it answerable
        known = known_areas(orch.server)
        if not area:
            options = ", ".join(known)
            msg = (f"Which area did you mean? I serve {options}."
                   if options else "Which area did you mean?")
            return orch._clarify(msg)
        msg = action.message or orch._ask_which(service=service, area=None)
        return orch._clarify(msg)

    if name in ("hold_slot", "get_provider"):
        # A user who names a provider ("book prov_06 for tomorrow") makes holding
        # a legitimate opening step; so does a reschedule, which must hold the
        # target slot before the user is asked to approve the move.
        if args.get("provider_id"):
            if orch.session.state in (State.NEW, State.CLARIFYING, State.DISCOVERING):
                orch._transition(State.SELECTING)
            return orch._dispatch(action)
        msg = action.message or orch._ask_which(
            service=orch.session.slots.get("service"),
            area=orch.session.slots.get("area"))
        return orch._clarify(msg)

    # get_booking_status makes no sense before anything is in flight
    known = known_areas(orch.server)
    msg = action.message or (
        f"I need the service and area before I can search. I serve "
        f"{', '.join(known)} for plumbers, electricians and AC service."
        if known else "I need the service and area before I can search.")
    return orch._clarify(msg)
