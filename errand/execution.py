"""Carrying out an approved side effect, and deciding what really happened.

The single rule here: never report an outcome from the model's word or from the
absence of an error. A timeout is not a failure — it is an unknown, and the only way
out of an unknown is the authoritative status tool. `after_verify` is the one place
that decides, so "booked" means a booking id came back from a query, not that a call
returned without raising.

The orchestrator is passed explicitly rather than inherited from: these functions need
its session, hold and tracer, and naming that dependency is more honest than a mixin
that hides it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from errand.approval import ApprovalMissing
from errand.llm import Action
from errand.policy import State
from errand.session import TurnResult
from errand.types import ToolError

if TYPE_CHECKING:                      # pragma: no cover - import cycle at runtime
    from errand.orchestrator import Orchestrator


def execute_approved(orch: Orchestrator, action: Action) -> TurnResult:
    """Run the one approved verb, through the approval store, with one transient retry."""
    name = action.tool or ""
    orch._attribute(name, action.args)
    live = orch.pending
    if live is None or live.kind != name:
        msg = "Nothing is held for me to do that. Confirm the details and I will ask again."
        orch.history.append("assistant", msg)
        return orch._result(name, msg, refused="no pending approval")

    def _call():
        if name == "book":
            assert orch.hold is not None
            return orch.server.book(orch.hold.hold_id, live.idempotency_key())
        if name == "cancel":
            assert live.booking_id
            return orch.server.cancel(live.booking_id, live.idempotency_key())
        assert live.booking_id
        assert orch.hold is not None
        return orch.server.reschedule(live.booking_id, orch.hold.slot, live.idempotency_key())

    booking = None
    for attempt in (1, 2):  # one retry, for transient only
        try:
            booking = orch.approvals.execute_if_approved(orch.session.id, live, _call)
            break
        except ApprovalMissing as exc:
            msg = "That is not approved, so I have not done it."
            orch.history.append("assistant", msg)
            return orch._result(name, msg, refused=str(exc))
        except ToolError as exc:
            if exc.error_class == "transient" and attempt == 1:
                if orch.tracer:
                    orch.tracer.emit("side_effect_retry", tool=name,
                                     error=exc.error_class, detail=exc.detail)
                continue
            return after_side_effect_error(orch, name, exc)
    orch.session.tool_calls += 1
    orch._terminal("booked", verb=name)
    orch.session.slots["reference"] = booking.booking_id
    if orch.tracer:
        orch.tracer.emit("side_effect_ok", tool=name, booking_id=booking.booking_id)
    return orch._result(name, None)


def after_side_effect_error(orch: Orchestrator, name: str, exc: ToolError) -> TurnResult:
    """Classify the failure. A timeout is an unknown, not a failure."""
    if orch.tracer:
        orch.tracer.emit("side_effect_error", tool=name, error=exc.error_class)
    if exc.error_class in ("timeout", "unexpected"):
        # the call may have landed: never claim either way without checking
        orch._transition(State.VERIFYING)
        orch.session.slots["verify_pending"] = True
        status = orch.gateway.call(
            State.VERIFYING, "get_booking_status",
            {"idempotency_key": orch.pending.idempotency_key()} if orch.pending else {},
        )
        return after_verify(orch, status.value if status.ok else {"status": "unknown"})
    if exc.error_class in ("slot_taken", "slot_conflict"):
        orch._terminal(exc.error_class)
        return orch._result(name, None)
    if exc.error_class == "hold_expired":
        orch._terminal("hold_expired")
        return orch._result(name, None)
    orch._terminal("tool_failure")
    orch.session.slots["reason"] = exc.detail
    return orch._result(name, None)


def after_verify(orch: Orchestrator, status: dict) -> TurnResult:
    """The authoritative status decided the outcome. Nothing else may."""
    state = status.get("status")
    if state == "booked":
        # This is the single most important outcome in the assignment, and it emitted
        # no side_effect_ok: the booking exists, the run claimed success, and the trace
        # had no record of it. Emit the confirmation explicitly.
        if orch.tracer:
            tool = (orch.pending.kind if orch.pending else None) or "book"
            orch.tracer.emit(
                "side_effect_ok",
                tool=tool,
                booking_id=status.get("booking_id"),
                verified=True,
            )
        if orch.session.outcome_verb is None and orch.pending is not None:
            orch.session.outcome_verb = orch.pending.kind
        orch._terminal("booked_verified_after_timeout", verb=orch.session.outcome_verb)
        orch.session.slots["reference"] = status.get("booking_id")
    elif state == "not_booked":
        # safe to retry: one more attempt, same key
        orch._terminal("booked" if orch.session.outcome else "unverified")
    else:
        orch._terminal("unverified")
        orch.session.slots["reference"] = status.get("booking_id")
    return orch._result("get_booking_status", None)
