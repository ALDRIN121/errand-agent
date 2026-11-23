"""Questions about providers, and what the world offers instead.

Every question this agent asks names what it can offer. "Which provider do you mean?"
is unanswerable unless the user already knows the inventory — the question hands the
work straight back, which is the same failure as an empty result with no alternatives.
"""
from __future__ import annotations


def providers(server) -> list:
    return list((getattr(server, "_providers", None) or {}).values())


def elsewhere(server, *, area: str | None, service: str | None) -> str:
    """What the world does serve, phrased as an offer the user can act on.

    Read from the provider table, not from a hand-written map: the point is to
    describe the one world this run is actually against.
    """
    rows = providers(server)
    if not rows:
        return ""
    by_area: dict[str, set[str]] = {}
    for p in rows:
        by_area.setdefault(p.area, set()).add(p.service)
    lines = []
    for other in sorted(by_area):
        if area and other == area:
            continue                    # they already know about this one
        services = ", ".join(sorted(s.replace("_", " ") for s in by_area[other]))
        lines.append(f"{other} ({services})")
    if not lines:
        return ""
    return "; ".join(lines[:4])


def ask_which(server, *, service: str | None, area: str | None,
              what: str = "provider") -> str:
    """Ask for a choice, and put the real choices in the question."""
    rows = [p for p in providers(server)
            if (service is None or p.service == service)
            and (area is None or p.area == area)]
    rows.sort(key=lambda p: (-p.rating, p.price_paise, p.id))
    if not rows:
        alternatives = elsewhere(server, area=area, service=service)
        if alternatives:
            return (f"I have no {service or 'provider'} in {area or 'that area'} to "
                    f"offer. I can offer {alternatives}. Which would you like?")
        return "Which area and service did you have in mind?"
    lines = [f"{p.id}: {p.service.replace('_', ' ')} in {p.area}, rating {p.rating}, "
             f"Rs {p.price_paise // 100:,}" for p in rows[:5]]
    options = "\n".join(lines)
    return f"Which {what} did you mean? I have:\n{options}"
