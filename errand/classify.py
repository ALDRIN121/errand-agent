"""Is this reply a yes? A pure function, with no model in the loop.

Deliberately not an LLM call. The approval path must not run through the same
untrusted model it exists to constrain, and a three-way split over a yes/no turn
does not need one.

The policy is conservative by construction: negation beats affirmation, a question
is never a yes, and any new constraint is treated as a negotiation rather than a
confirmation. "yes but not before 10" is therefore not an approval, which is the
case that matters -- a naive substring match on "yes" would book the wrong slot.
"""
from __future__ import annotations

import re
from typing import Literal

Verdict = Literal["affirm", "deny", "other"]

AFFIRM = frozenset(
    {
        "yes", "yeah", "yep", "yup", "ok", "okay", "go ahead", "go for it",
        "book it", "book", "do it", "confirm", "confirmed", "proceed", "sure",
        "correct", "that's right", "thats right", "sounds good", "fine",
    }
)

NEGATE = frozenset(
    {
        "no", "nope", "not", "don't", "dont", "do not", "stop", "cancel",
        "wait", "hold on", "later", "never", "wrong",
    }
)

_PUNCT = re.compile(r"[^\w\s:'₹]")
_SPACE = re.compile(r"\s+")
_CLOCK_TIME = re.compile(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b|\b\d{1,2}:\d{2}\b")
_AMOUNT = re.compile(r"[₹$]\s?\d|\b\d{3,}\b")

# Words that carry no new information once a reply is already an affirmative:
# connectives, politeness, and the affirmative phrases themselves. A reply made
# entirely of these is an unambiguous yes.
_FILLER = frozenset(
    {
        "please", "pls", "thanks", "thank", "you", "and", "then", "so", "it",
        "that", "this", "is", "fine", "good", "sounds", "go", "ahead", "for",
        "right", "correct", "do", "book", "confirm", "confirmed", "proceed",
        "sure", "ok", "okay", "yes", "yeah", "yep", "yup",
    }
)


def normalise(text: str) -> str:
    t = text.strip().lower()
    t = t.replace("’", "'")
    t = _PUNCT.sub(" ", t)
    t = _SPACE.sub(" ", t).strip()
    return t


def has_new_constraint(text: str) -> bool:
    """A new time, amount or explicit provider reference is a change, not a yes.

    The amount pattern matters: "yes, if it's under 2000" must not approve a
    ₹4,999 booking.
    """
    return bool(_CLOCK_TIME.search(text) or _AMOUNT.search(text))


def classify(reply: str) -> Verdict:
    raw = reply or ""
    if not raw.strip():
        return "other"

    norm = normalise(raw)
    tokens = set(norm.split())
    # multi-word negations ("hold on", "do not") survive normalise, so check both
    if tokens & NEGATE or any(p in norm for p in ("hold on", "do not", "don't")):
        return "deny"
    if "?" in raw or "?" in norm:
        return "other"
    if has_new_constraint(raw):
        return "other"
    if norm in AFFIRM:
        return "affirm"
    # Multi-word affirmative phrases ("sounds good", "go ahead") cannot match a
    # token set, so test them against the normalised string first.
    phrases = {p for p in AFFIRM if " " in p and p in norm}
    if phrases:
        residue = norm
        for p in sorted(phrases, key=len, reverse=True):
            residue = residue.replace(p, " ")
        if set(residue.split()) <= _FILLER:
            return "affirm"
    # An affirmative padded with connectives and politeness ("yep, go ahead",
    # "yes please") is still a yes. Anything with a word outside that filler set
    # is a new instruction, so it stays ambiguous.
    if tokens & AFFIRM and tokens <= (_FILLER | AFFIRM):
        return "affirm"
    return "other"
