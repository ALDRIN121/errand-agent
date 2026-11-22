"""Phase 2 gate, part 3: the reply classifier.

The cases that matter are the near-misses. A substring match on "yes" books the
wrong slot when the user says "yes but not before 10".
"""
from __future__ import annotations

import pytest

from errand.classify import classify, has_new_constraint, normalise

CASES = [
    # explicit affirmatives
    ("yes", "affirm"),
    ("Yes", "affirm"),
    ("YES.", "affirm"),
    ("yeah", "affirm"),
    ("yep, go ahead", "affirm"),
    ("ok", "affirm"),
    ("okay", "affirm"),
    ("sure", "affirm"),
    ("book it", "affirm"),
    ("do it", "affirm"),
    ("confirm", "affirm"),
    ("proceed", "affirm"),
    ("sounds good", "affirm"),
    ("that's right", "affirm"),
    # explicit negatives
    ("no", "deny"),
    ("nope", "deny"),
    ("don't", "deny"),
    ("do not book it", "deny"),
    ("stop", "deny"),
    ("cancel that", "deny"),
    ("wait", "deny"),
    ("hold on", "deny"),
    ("no thanks", "deny"),
    # near-misses: must not approve
    ("yes but not before 10", "deny"),
    ("ok but is he any good?", "other"),
    ("yes, can you make it 10am?", "other"),
    ("sure, if it's under 2000", "other"),
    ("maybe", "other"),
    ("", "other"),
    ("   ", "other"),
    ("what does that include?", "other"),
    ("go ahead and book the cheaper one", "other"),
    ("no, try the other provider", "deny"),
    ("yes please, and add a note", "other"),
    # affirmatives padded with politeness and connectives are still yes
    ("yep, go ahead", "affirm"),
    ("yes please", "affirm"),
    ("sounds good, thanks", "affirm"),
    ("ok then, do it", "affirm"),
]


@pytest.mark.parametrize("reply,expected", CASES)
def test_classify_table(reply, expected):
    assert classify(reply) == expected, reply


def test_negation_beats_affirmation():
    assert classify("yes but not before 10") == "deny"


def test_question_is_never_a_yes():
    assert classify("ok but is he any good?") == "other"


def test_amount_constraint_is_not_a_bare_yes():
    """A conditional yes must not approve a price the user just capped."""
    assert classify("sure, if it's under 2000") == "other"
    assert classify("yes if under 2000") == "other"


def test_new_constraint_detects_times_and_amounts():
    assert has_new_constraint("at 10am")
    assert has_new_constraint("before 10:30")
    assert has_new_constraint("under 1500")
    assert not has_new_constraint("go ahead")


def test_normalise_is_stable():
    assert normalise("  YES.  ") == "yes"
    assert normalise("Go ahead!") == "go ahead"


def test_empty_and_whitespace_are_other():
    assert classify("") == "other"
    assert classify("   ") == "other"
    assert classify(None) == "other"
