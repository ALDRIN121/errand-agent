"""What the world serves, and how a typed area resolves against it.

The vocabulary is derived from the provider table, never hand-written: a second copy
of the map drifts, and this list is what tells a shorthand ("Kora") from a typo.
"""
from __future__ import annotations


def known_areas(server) -> list[str]:
    """The areas this world actually serves, derived from the providers."""
    providers = getattr(server, "_providers", None) or {}
    return sorted({p.area for p in providers.values()})


def resolve_area(typed: str, known: list[str]) -> tuple[str | None, list[str]]:
    """Match what a person typed against the areas we serve.

    Returns (exact, candidates). `exact` is set only when the answer is not a guess:
    a case-insensitive match, or a unique prefix. "Kora" is unambiguous; "K" is not,
    and a wrong guess books a plumber in the wrong neighbourhood, which is worse than
    asking. Guessing also cannot be undone by the user saying "no" — an approval built
    on a guessed area would ask about a different booking than the one they wanted.

    The vocabulary is a lookup of the areas the world serves, not language
    understanding, in the same spirit as `service_from`.
    """
    text = (typed or "").strip().lower()
    if not text:
        return None, known
    for area in known:                        # exact, case-insensitive
        if area.lower() == text:
            return area, []
    # A one- or two-letter prefix is not a word anyone types in earnest, and matching
    # it would let "a" resolve to whichever area happens to start with it. Three is
    # enough to distinguish real shorthands ("Kor", "HSR", "Ind") from noise.
    prefixed = [a for a in known if len(text) >= 3 and a.lower().startswith(text)]
    if len(prefixed) == 1:
        return prefixed[0], []
    # Substring matching needs the same floor: "k" is inside "Koramangala" and inside
    # nothing else, which is a coincidence, not an intent.
    contains = [a for a in known if len(text) >= 3 and text in a.lower()]
    if len(contains) == 1:
        return contains[0], []
    return None, (prefixed or contains or known)
