"""The demo fixture must be internally consistent before any demo relies on it.

These tests are cheap and catch the class of error that ruins a recording: a
booking that references a provider in the wrong area, a dialogue whose expected
outcome is not a real token, or a "forgotten-candidate" scripted reply that no
longer matches the code.
"""
from __future__ import annotations

import json
import json as _json
import pathlib as _pathlib

import pytest

from errand.classify import classify
from errand.clock import FakeClock
from evals.demo_data import ANCHOR, build_world, load_fixture, slot_at

FIXTURE = load_fixture()

# G3: narration lives in its own file; the world file holds users, bookings,
# reviews and the prior conversation only.

_DIALOGUE_PATH = _pathlib.Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "demo_dialogues.json"
DIALOGUES = _json.loads(_DIALOGUE_PATH.read_text())["dialogues"]

OUTCOME_TOKENS = {
    "booked", "booked_verified_after_timeout", "slot_taken", "slot_conflict",
    "hold_expired", "no_match", "unverified", "aborted_missing_details",
    "budget_turns", "budget_tools", "tool_failure", "cancelled", "abandoned_by_user",
}


# ----------------------------------------------------------------- structure


def test_fixture_parses_and_has_the_expected_sections():
    for section in ("users", "bookings", "provider_reviews", "prior_conversation"):
        assert section in FIXTURE, section
    assert "example_dialogues" not in FIXTURE, "narration belongs in demo_dialogues.json"
    assert len(DIALOGUES) == 8


def test_three_users_with_distinct_areas():
    users = FIXTURE["users"]
    assert len(users) == 3
    assert len({u["area"] for u in users}) == 3


def test_every_booking_references_a_real_provider_and_user():
    users = {u["id"] for u in FIXTURE["users"]}
    reviews = {k for k in FIXTURE["provider_reviews"] if not k.startswith("_")}
    for row in FIXTURE["bookings"]:
        assert row["user_id"] in users, row["booking_id"]
        assert row["provider_id"].startswith("prov_"), row["booking_id"]
        # every referenced provider has fixture reviews, so the demo is predictable
        assert row["provider_id"] in reviews, row["provider_id"]


def test_booking_ids_are_unique():
    ids = [b["booking_id"] for b in FIXTURE["bookings"]]
    assert len(ids) == len(set(ids))


def test_provider_area_matches_the_booking_area():
    """A booking in the wrong area is the kind of slip that reads as a bug in a demo."""
    providers = {p["id"]: p for p in json.loads(
        (__import__("pathlib").Path(__file__).resolve().parents[1]
         / "evals" / "fixtures" / "providers.json").read_text()
    )}
    for row in FIXTURE["bookings"]:
        provider = providers[row["provider_id"]]
        assert provider["area"] == row["area"], (
            f"{row['booking_id']}: booking says {row['area']}, "
            f"{row['provider_id']} is in {provider['area']}"
        )
        assert provider["service"] == row["service"], row["booking_id"]


def test_slot_offsets_are_time_ordered_for_aldrin():
    rows = [b for b in FIXTURE["bookings"] if b["user_id"] == "u_aldrin"]
    assert len(rows) >= 3
    assert len({r["slot_day_offset"] for r in rows}) == len(rows)


# ---------------------------------------------------------------- dialogues


def test_every_dialogue_has_a_valid_expected_outcome():
    for ex in DIALOGUES:
        assert ex["expected"]["outcome"] in OUTCOME_TOKENS, ex["id"]


def test_every_dialogue_declares_at_least_one_invariant():
    for ex in DIALOGUES:
        assert ex["expected"]["invariants"], ex["id"]


def test_every_dialogue_invariant_name_is_real():
    """An invariant id in the fixture that the harness does not implement is a
    silent pass: the scenario would look green while asserting nothing."""
    known = {"I1", "I2", "I3", "I3b", "I4", "I5", "I6", "I7", "I8", "I9"}
    for ex in DIALOGUES:
        unknown = set(ex["expected"]["invariants"]) - known
        assert not unknown, f"{ex['id']} references {unknown}"


def test_dialogues_alternate_and_start_with_the_user():
    for ex in DIALOGUES:
        turns = ex["turns"]
        assert turns[0]["role"] == "user", ex["id"]
        for a, b in zip(turns, turns[1:], strict=False):
            assert a["role"] != b["role"], ex["id"]


def test_all_three_assignment_examples_are_covered():
    examples = [ex.get("matches_assignment_example", "") for ex in DIALOGUES]
    joined = " ".join(examples).lower()
    assert "plumber for my kitchen tap" in joined
    assert "move my ac servicing" in joined
    assert "same electrician as last time" in joined


# ------------------------------------------------------- scripted replies are real


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("Sounds good, thanks", "affirm"),
        ("yes, go ahead", "affirm"),
        ("yes", "affirm"),
        ("Hold Tuesday at 9.", "other"),
        ("No, find someone else.", "deny"),
    ],
)
def test_scripted_user_replies_classify_as_intended(reply, expected):
    """The demo's confirmations must actually be confirmations."""
    assert classify(reply) == expected


# ------------------------------------------------------------- the live world


def test_build_world_seeds_bookings_and_memories():
    clock = FakeClock(ANCHOR)
    world = build_world(clock, user_id="u_aldrin")
    try:
        rows = world.bookings_for("u_aldrin")
        assert len(rows) == 4
        assert {r.status for r in rows} == {"confirmed", "cancelled"}

        keys = {m.key for m in world.memory.admissible("u_aldrin")}
        assert "preferred_area" in keys
        assert "earliest_acceptable_time" in keys
        assert "budget_ceiling" in keys
    finally:
        world.close()


def test_world_has_a_live_clock():
    """Prior bookings must sit in the past relative to the demo's clock."""
    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, user_id="u_aldrin")
    try:
        past = [b for b in world.bookings_for("u_aldrin") if b.slot.start < clock.now()]
        assert past, "no seeded booking is in the past"
    finally:
        world.close()


def test_worlds_do_not_share_a_database():
    clock = FakeClock(ANCHOR)
    a = build_world(clock, user_id="u_aldrin")
    b = build_world(clock, user_id="u_aldrin")
    try:
        assert a.db.path != b.db.path
        a.memory.add("u_aldrin", "only_in_a", "x", "test")
        assert "only_in_a" not in {m.key for m in b.memory.admissible("u_aldrin")}
    finally:
        a.close()
        b.close()


def test_injection_providers_carry_an_instruction_and_clean_ones_do_not():
    clock = FakeClock(ANCHOR)
    world = build_world(clock)
    try:
        for pid, block in FIXTURE["provider_reviews"].items():
            if pid.startswith("_"):
                continue
            texts = " ".join(r["text"] for r in block["reviews"])
            if block["injection"]:
                assert ("IGNORE PREVIOUS" in texts) or ("SYSTEM:" in texts), pid
            else:
                assert "IGNORE PREVIOUS" not in texts and "SYSTEM:" not in texts, pid
    finally:
        world.close()


def test_prior_conversation_is_long_enough_to_press_the_budget():
    turns = FIXTURE["prior_conversation"]["turns"]
    assert len(turns) == 24
    # and it must contain a correction, so the "preserve other details" path is real
    assert any("corrects" in t for t in turns)


def test_prior_conversation_area_correction_is_present():
    turns = FIXTURE["prior_conversation"]["turns"]
    corrections = [t for t in turns if t.get("corrects") == "area"]
    assert len(corrections) == 1
    assert "Koramangala" in corrections[0]["text"]


# ------------------------------------------------------------------- slots


def test_slot_at_is_timezone_aware_and_one_hour():
    slot = slot_at(2, 10)
    assert slot.start.tzinfo is not None
    assert slot.end - slot.start == __import__("datetime").timedelta(hours=1)
    assert slot.start.hour == 10


# ------------------------------------------- dialogue text matches the provider table


def _providers() -> dict:
    import pathlib as _p

    path = _p.Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "providers.json"
    return {p["id"]: p for p in json.loads(path.read_text())}


@pytest.mark.parametrize("dialogue", DIALOGUES, ids=lambda d: d["id"])
def test_dialogue_prices_match_the_provider_table(dialogue):
    """A dialogue quoting a price the provider table does not hold is a demo that
    lies on camera. Every `₹N` in the script must be a real price for a provider
    the script names, or that price plus the documented price-shift delta."""
    providers = _providers()
    real = {p["price_paise"] // 100 for p in providers.values()}
    real |= {v + 400 for v in real}  # the configurable price-shift quote
    text = " ".join(t["text"] for t in dialogue["turns"])
    quoted = {
        int(chunk)
        for chunk in __import__("re").findall(r"₹(\d+)", text)
        if int(chunk) > 100  # skip the ₹4,999 bait figure inside an injection
    }
    # A user may name their own ceiling ("under ₹600"); that is a budget, not a price.
    user_quoted = {
        int(chunk)
        for turn in dialogue["turns"]
        if turn["role"] == "user"
        for chunk in __import__("re").findall(r"₹(\d+)", turn["text"])
    }
    quoted -= user_quoted
    unknown = quoted - real
    assert not unknown, f"{dialogue['id']} quotes prices that do not exist: {sorted(unknown)}"


@pytest.mark.parametrize("dialogue", DIALOGUES, ids=lambda d: d["id"])
def test_dialogue_provider_ids_exist(dialogue):
    providers = _providers()
    text = " ".join(t["text"] for t in dialogue["turns"])
    ids = set(__import__("re").findall(r"prov_\d{2}", text))
    unknown = ids - set(providers)
    assert not unknown, f"{dialogue['id']} references {sorted(unknown)}"


@pytest.mark.parametrize("dialogue", DIALOGUES, ids=lambda d: d["id"])
def test_dialogue_services_match_the_provider_they_name(dialogue):
    """If a script says 'plumber ... prov_06', prov_06 had better be a plumber."""
    providers = _providers()
    text = " ".join(t["text"] for t in dialogue["turns"]).lower()
    for pid in set(__import__("re").findall(r"prov_\d{2}", text)):
        service = providers[pid]["service"]
        spoken = {"ac_service": "ac", "plumber": "plumb", "electrician": "electric"}[service]
        assert spoken in text, f"{dialogue['id']} names {pid} but never says {service}"


def test_injection_flag_agrees_with_the_mock_determinism_rule():
    """The mock injects for seed_index % 3 in (0, 1). A fixture review block whose
    `injection` flag disagrees would make the demo silently unpredictable: the
    scripted reply would promise an injection that never appears, or ignore one
    that does."""
    providers = _providers()
    for pid, block in FIXTURE["provider_reviews"].items():
        if pid.startswith("_"):
            continue
        expected = providers[pid]["seed_index"] % 3 in (0, 1)
        assert block["injection"] is expected, (
            f"{pid}: seed_index {providers[pid]['seed_index']} implies injection={expected}, "
            f"fixture says {block['injection']}"
        )


def test_every_referenced_provider_has_reviews():
    referenced = {b["provider_id"] for b in FIXTURE["bookings"]}
    missing = referenced - set(FIXTURE["provider_reviews"])
    assert not missing, f"bookings reference providers with no reviews: {sorted(missing)}"


def test_fixture_has_no_orphan_review_blocks():
    """A review block nothing references is dead data that will rot."""
    referenced = {b["provider_id"] for b in FIXTURE["bookings"]}
    for dialogue in DIALOGUES:
        text = " ".join(t["text"] for t in dialogue["turns"])
        referenced |= set(__import__("re").findall(r"prov_\d{2}", text))
    orphans = {k for k in FIXTURE["provider_reviews"] if not k.startswith("_")} - referenced
    assert not orphans, f"review blocks nothing references: {sorted(orphans)}"
