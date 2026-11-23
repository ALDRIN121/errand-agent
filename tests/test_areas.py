"""What happens to an area a person typed that we do not serve.

"Kora" is not a typo, it is a shorthand. Passing it to the tool produced "no provider
matches those constraints" for a user who was one word short, and the reason it offered
was a sentence about a budget that was never the problem.
"""

import pytest

from errand.orchestrator import known_areas, resolve_area

KNOWN = ["HSR Layout", "Indiranagar", "Koramangala"]


@pytest.mark.parametrize("typed,expected", [
    ("Koramangala", "Koramangala"),
    ("koramangala", "Koramangala"),      # case is not a mistake
    ("KORA", "Koramangala"),             # shorthand or shouting
    ("Kora", "Koramangala"),
    ("  Kora  ", "Koramangala"),         # stray whitespace
    ("Kora mangala", None),              # not a prefix; ask rather than guess
    ("HSR", "HSR Layout"),               # prefix
    ("hsr layout", "HSR Layout"),
    ("Indira", "Indiranagar"),
    ("K", None),                         # too short to be a shorthand
    ("", None),
    ("Whitefield", None),                # a real area we do not serve
])
def test_an_ambiguous_or_unknown_area_is_never_guessed(typed, expected):
    resolved, candidates = resolve_area(typed, KNOWN)
    assert resolved == expected, (typed, resolved, candidates)


def test_a_failed_match_offers_what_is_available():
    """"Whitefield" must come back with something the user can pick."""
    resolved, candidates = resolve_area("Whitefield", KNOWN)
    assert resolved is None
    assert candidates == KNOWN


def test_a_too_short_prefix_offers_the_contenders_instead_of_guessing():
    resolved, candidates = resolve_area("K", KNOWN)
    assert resolved is None
    assert "Koramangala" in candidates


def test_a_real_shorthand_still_resolves():
    """The three-character rule must not break "Kor" or "Ind"."""
    assert resolve_area("Kor", KNOWN)[0] == "Koramangala"
    assert resolve_area("Ind", KNOWN)[0] == "Indiranagar"


def test_a_resolved_area_is_the_spelling_the_world_uses():
    """The provider table uses one spelling; a booking must agree with it."""
    resolved, _ = resolve_area("koramangala", KNOWN)
    assert resolved == "Koramangala"


def test_the_known_areas_are_derived_from_the_providers():
    """A second hand-written copy of the map would drift from the first."""
    import datetime

    from errand.clock import FakeClock
    from evals.demo_data import build_world
    world = build_world(FakeClock(datetime.datetime(2026, 11, 23, 8, 0)),
                        seed=7, scenario="happy", user_id="u_aldrin")
    areas = known_areas(world.server)
    assert areas == sorted({p.area for p in world.server._providers.values()})
    assert areas, "the world must serve somewhere"
