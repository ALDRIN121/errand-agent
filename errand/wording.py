"""Text the user or the model reads.

Two audiences, one rule: nothing here decides anything. These functions render a fact
that already exists — a missing slot, a held slot, a tool result — so the same fact is
worded the same way everywhere. A formatter that can change behaviour is a second
decision point.
"""
from __future__ import annotations

from typing import Any

from errand.approval import PendingAction
from errand.types import Hold

_SLOT_NAMES = {
    "service": "the service",
    "area": "the area",
    "window_start": "the earliest time",
    "window_end": "the latest time",
}

SERVICE_WORDS = {
    "ac_service": ("ac", "a/c", "air con", "aircon", "servicing"),
    "plumber": ("plumb", "tap", "leak", "pipe"),
    "electrician": ("electric", "wiring", "switchboard", "mcb", "socket"),
}

MANAGE_VERBS = ("move", "cancel", "reschedule", "change", "shift", "postpone")


def plain_slots(names: list[str]) -> str:
    """Slot identifiers read as English. "window_start" is a name for a dict key, and a
    user is owed a sentence rather than a schema."""
    words = [_SLOT_NAMES.get(n, n.replace("_", " ")) for n in names]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def service_from(text: str) -> str | None:
    """The service named in the user's words, or None.

    A closed vocabulary, deliberately: this is a lookup of the three contract
    services, not natural-language understanding. Ambiguous text returns None and
    falls through to the ladder's question.
    """
    low = (text or "").lower()
    hits = [svc for svc, words in SERVICE_WORDS.items() if any(w in low for w in words)]
    return hits[0] if len(hits) == 1 else None


def is_manage_request(text: str) -> bool:
    """Whether the user asked to modify an existing booking rather than make one."""
    low = (text or "").lower()
    return any(v in low for v in MANAGE_VERBS)


def action_message_for(hold: Hold) -> str:
    """The sentence shown when a slot is held, quoted price included.

    The quoted price is the point: a price that moved between search and hold is
    visible here, before the user approves, rather than a digest mismatch afterwards.
    """
    return (
        f"Hold {hold.provider_id}, {hold.slot.start:%a %d %b %H:%M}-{hold.slot.end:%H:%M}, "
        f"quoted Rs {hold.quote_paise // 100:,}. Confirm? The hold expires in 10 minutes."
    )


def default_prompt(pending: PendingAction) -> str:
    """What the approval asks, naming every material detail that was frozen."""
    if pending.kind == "cancel":
        return f"Cancel booking {pending.booking_id} with {pending.provider_id}? No charge."
    if pending.kind == "reschedule":
        return (
            f"Reschedule {pending.booking_id} to {pending.slot_start:%a %d %b %H:%M}"
            f"-{pending.slot_end:%H:%M} with {pending.provider_id}. Confirm?"
        )
    return (
        f"Book {pending.service} with {pending.provider_id}, "
        f"{pending.slot_start:%a %d %b %H:%M}-{pending.slot_end:%H:%M}, "
        f"quoted Rs {pending.price_paise // 100:,}. Confirm?"
    )


def summarise(name: str, value: Any) -> str | None:
    """A compact, model-readable rendering of a read result.

    The context budget allows 500 tokens for a tool result, so the full search payload
    cannot be replayed; the fields a choice needs are the id, the price, the rating and
    the first free slot.
    """
    if value is None:
        return None
    if name == "search_providers":
        rows = value.get("providers") if isinstance(value, dict) else value
        if not rows:
            return "No providers matched."
        lines = []
        for p in rows[:8]:
            free = [s for s in getattr(p, "slots", []) if s.available]
            if free:
                # The days this provider can actually serve, not just the next slot.
                # "next free Mon 23 06:00" reads as "only Monday", which is what made
                # the model tell the user no Saturday existed while Saturday slots
                # were sitting in the record.
                days = list(dict.fromkeys(f"{s.start:%a %d %b}" for s in free))
                shown = ", ".join(days[:5]) + (f" (+{len(days) - 5})" if len(days) > 5 else "")
                when = f"free {shown}"
            else:
                when = "no free slot"
            lines.append(f"- {p.id}: rating {p.rating}, Rs {p.price_paise // 100:,}, {when}")
        return "Providers found:\n" + "\n".join(lines)
    if name == "get_provider":
        p = value
        free = [s for s in getattr(p, "slots", []) if s.available]
        lines = [f"{p.id}: rating {p.rating}, Rs {p.price_paise // 100:,}, "
                 f"serves {p.service} in {p.area}"]
        if free:
            # Grouped by day, one line per day. The flat first-six listing showed a
            # whole week's worth of Monday morning and nothing else, so a model asked
            # "does this provider have Saturday?" could only answer no — the Saturday
            # slots existed and were never shown. A day-per-line summary fits the
            # seven-day window in the same number of lines and is the shape the
            # question is actually asked in.
            lines.append("free slots by day (pass start+end to hold_slot):")
            by_day: dict[str, list] = {}
            for slot in free:
                by_day.setdefault(f"{slot.start:%a %d %b}", []).append(slot)
            for day, slots in list(by_day.items())[:7]:
                hours = ", ".join(f"{s.start:%H:%M}" for s in slots[:8])
                more = f" (+{len(slots) - 8} more)" if len(slots) > 8 else ""
                lines.append(f"- {day}: {hours}{more}")
            if len(by_day) > 7:
                lines.append(f"  (+{len(by_day) - 7} more days)")
        else:
            lines.append("no free slots")
        return "\n".join(lines)
    if name == "list_bookings":
        rows = value if isinstance(value, list) else value.get("bookings", [])
        if not rows:
            return "No bookings on record."
        return "Bookings:\n" + "\n".join(
            f"- {b.booking_id}: {b.service} with {b.provider_id} "
            f"{b.slot.start:%a %d %b %H:%M} ({b.status})"
            for b in rows[:8]
        )
    if name == "get_booking_status":
        return f"status: {value.get('status')}, booking {value.get('booking_id')}"
    return None
