"""Choosing a replacement when a provider cancels. A sort, not a judgement.

This runs with no human in the loop, so it is the one path where a model call would add
cost and nondeterminism for no benefit. The ranking is total and explainable:
preferred technician when the user has stated one, otherwise rating, then price, then
id — every tie broken, so the same world always yields the same choice.
"""
from __future__ import annotations

from errand.types import Provider, Slot


def find_replacement(server, memory, user_id: str, cancelled) -> tuple[Provider, Slot] | None:
    """Return (provider, slot) for the best replacement, or None."""
    pinned = {m.key: m.value for m in memory.admissible(user_id)}.get("preferred_technician")

    candidates = [
        p for p in server.search_providers(
            service=cancelled.service, area=cancelled.area
        )
        if p.id != cancelled.provider_id
    ]
    if not candidates:
        return None

    def slot_for(p: Provider) -> Slot | None:
        return next(
            (s for s in p.slots
             if s.available and s.start.date() >= cancelled.slot.start.date()),
            None,
        )

    with_slot = [(p, s) for p in candidates if (s := slot_for(p)) is not None]
    if not with_slot:
        return None

    if pinned:
        chosen = next((pair for pair in with_slot if pair[0].id == pinned), None)
        if chosen is not None:
            return chosen

    with_slot.sort(key=lambda pair: (-pair[0].rating, pair[0].price_paise, pair[0].id))
    return with_slot[0]
