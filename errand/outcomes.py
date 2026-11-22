"""Terminal sentences. One function, one sentence per outcome.

`I3b` requires the final user-facing sentence to be byte-identical across runs,
which is impossible if the model writes it. So the model never writes it: it
reaches `TERMINAL`, the orchestrator stores a token, and this module renders the
sentence from the stored action.

The `manage_verb` distinction matters: `booked` covers a successful book, cancel
and reschedule, so the token alone cannot tell the user what happened.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from errand.approval import PendingAction

OUTCOMES = (
    "booked",
    "booked_verified_after_timeout",
    "slot_taken",
    "slot_conflict",
    "hold_expired",
    "no_match",
    "unverified",
    "aborted_missing_details",
    "tool_failure",
    "abandoned_by_user",
    "budget_turns",
    "budget_tools",
)


@dataclass(frozen=True)
class OutcomeContext:
    """Everything a sentence may legitimately reference.

    Passed in rather than read from a global so `render_outcome` stays a pure
    function of its inputs — the property that makes the byte-equality test mean
    something.
    """

    action: PendingAction | None = None
    provider_name: str | None = None
    reference: str | None = None
    reason: str | None = None


def _when(action: PendingAction | None) -> str:
    if action is None or action.slot_start is None:
        return "the requested slot"
    return f"{action.slot_start:%a %d %b %H:%M}-{action.slot_end:%H:%M}"


def _price(action: PendingAction | None) -> str:
    if action is None or action.price_paise is None:
        return "the quoted price"
    return f"Rs {action.price_paise // 100:,}"


def render_outcome(token: str, ctx: OutcomeContext | None = None) -> str:
    """The single mapping from outcome token to user-facing sentence."""
    ctx = ctx or OutcomeContext()
    action = ctx.action
    verb = action.kind if action is not None else None

    if token == "booked":
        if verb == "cancel":
            return f"Cancelled {ctx.reference or 'the booking'}. No charge."
        if verb == "reschedule":
            return (
                f"Moved: {action.service} with {ctx.provider_name or action.provider_id}, "
                f"now {_when(action)}. Ref {ctx.reference or action.booking_id}."
            )
        if action is not None:
            return (
                f"Booked: {action.service} with {ctx.provider_name or action.provider_id}, "
                f"{_when(action)}, {_price(action)}. Ref {ctx.reference or 'confirmed'}."
            )
        return "Booked. Ref confirmed."

    if token == "booked_verified_after_timeout":
        if verb == "cancel":
            return (
                "Cancelled. The first attempt timed out, so I checked before "
                "telling you. No charge."
            )
        if action is not None:
            return (
                f"Booked: {action.service} with {ctx.provider_name or action.provider_id}, "
                f"{_when(action)}, {_price(action)}. The first attempt timed out, so I "
                f"checked before telling you. Ref {ctx.reference or 'confirmed'}."
            )
        return "Booked. The first attempt timed out, so I checked before telling you."

    if token == "slot_taken":
        return (
            f"That slot was taken while I was holding it. Nothing has been booked and "
            f"you have not been charged. {ctx.reason or 'I can show you other times.'}"
        )

    if token == "slot_conflict":
        return (
            f"That time clashes with something already booked. Nothing has been changed. "
            f"{ctx.reason or 'Pick another time and I will try again.'}"
        )

    if token == "hold_expired":
        return (
            "The hold on that slot expired before you replied, so nothing was booked "
            "and you have not been charged. Ask me again and I will re-check the price "
            "and availability."
        )

    if token == "no_match":
        return (
            f"No provider matches those constraints. {ctx.reason or 'Widening the '
            'budget, the area or the day would give me something to offer.'}"
        )

    if token == "unverified":
        return (
            f"I could not confirm whether that went through, so I am not going to claim "
            f"it did. Check ref {ctx.reference or 'the booking'} before relying on it."
        )

    if token == "aborted_missing_details":
        return (
            f"I stopped without booking anything: {ctx.reason or 'some details are still missing'}. "
            f"Nothing has been charged."
        )

    if token == "tool_failure":
        return (
            f"A service call failed, so nothing was booked. {ctx.reason or ''}".strip()
        )

    if token == "abandoned_by_user":
        return f"Stopped at your request. Nothing has been booked or charged."

    if token == "budget_turns":
        return (
            "I have used my turn budget for this request without finishing, so nothing "
            "was booked. Ask me again and I will pick up where I left off."
        )

    if token == "budget_tools":
        return (
            "I have used my tool-call budget for this request without finishing, so "
            "nothing was booked. Narrowing the request will let me finish."
        )

    raise ValueError(f"unknown outcome token {token!r}")


def is_terminal_token(token: str) -> bool:
    return token in OUTCOMES
