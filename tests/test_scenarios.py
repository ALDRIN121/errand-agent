"""Guards on the scenario set itself.

`evals/scenarios.py` is the assertion set for the whole project, so its own shape
needs checking before any agent code runs: 40 scenarios, contiguous ids, valid
expected tokens, and every outcome token exercised by at least one scenario.
"""
from __future__ import annotations

import pytest

from evals.demo_data import load_dialogues
from evals.scenarios import CORE, SCENARIOS, WATCHER

OUTCOME_TOKENS = {
    "booked", "booked_verified_after_timeout", "slot_taken", "slot_conflict",
    "hold_expired", "no_match", "unverified", "aborted_missing_details",
    "budget_turns", "budget_tools", "tool_failure", "cancelled", "abandoned_by_user",
}

MOCK_SCENARIOS = {
    "happy", "slot_taken", "timeout_booked", "timeout_not_booked", "double_unknown",
    "price_shift", "cancel_transient", "reschedule_conflict", "reschedule_ok",
    "provider_cancels", "recover_unavailable",
}


def test_exactly_40_scenarios():
    assert len(SCENARIOS) == 40


def test_ids_are_contiguous_from_one():
    assert [s.id for s in SCENARIOS] == list(range(1, 41))


def test_names_are_unique():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))


def test_every_expected_token_is_in_the_vocabulary():
    for s in SCENARIOS:
        unknown = s.expected - OUTCOME_TOKENS
        assert not unknown, f"scenario {s.id} expects unknown tokens {unknown}"


def test_every_outcome_token_is_exercised():
    """A token no scenario can produce is a terminal path nothing guards."""
    covered = set().union(*(s.expected for s in SCENARIOS))
    assert covered == OUTCOME_TOKENS, OUTCOME_TOKENS - covered


def test_every_scenario_uses_a_real_mock_scenario():
    for s in SCENARIOS:
        assert s.scenario in MOCK_SCENARIOS, f"{s.id}: {s.scenario}"


def test_every_scenario_names_a_known_user():
    known = {"u_aldrin", "u_priya", "u_rahul"}
    for s in SCENARIOS:
        assert s.user_id in known, f"{s.id}: {s.user_id}"


def test_core_plus_watcher_covers_everything():
    assert {s.id for s in CORE} | {s.id for s in WATCHER} == {s.id for s in SCENARIOS}
    assert not ({s.id for s in CORE} & {s.id for s in WATCHER})


def test_watcher_scenarios_are_the_two_that_need_phase_five():
    assert {s.id for s in WATCHER} == {23, 37}


def test_the_assignment_traps_are_all_present():
    """Every failure behaviour the brief names must appear in the scenario set."""
    names = " ".join(s.name for s in SCENARIOS).lower()
    required = {
        "adversarial input": ["premature", "out-of-scope"],
        "prompt injection": ["injection in review", "injection attempting"],
        "price change after approval": ["price change"],
        "slot change after approval": ["slot change"],
        "timeout after possible success": ["timeout-that-booked"],
        "slot conflict": ["slot conflict", "slot_taken"],
        "repeated confirmation": ["triple confirmation"],
        "memory correction": ["correction overwrites"],
        "memory forgetting": ["forget honoured"],
        "restart mid-booking": ["restart mid-booking"],
        "proactive cancellation": ["proactive replacement"],
        "context budget": ["context budget"],
        "limit exhaustion": ["budget exhaustion"],
        "tool failures": ["unexpected tool exception", "cancel 503"],
    }
    for trap, needles in required.items():
        assert any(n in names for n in needles), trap


@pytest.mark.parametrize("dialogue", load_dialogues(), ids=lambda d: d["id"])
def test_every_demo_dialogue_has_a_scenario(dialogue):
    """G3: the narration must not describe a run the harness never performs."""
    linked = {s.dialogue_id for s in SCENARIOS}
    assert dialogue["id"] in linked, dialogue["id"]


def test_every_dialogue_link_resolves():
    ids = {d["id"] for d in load_dialogues()}
    for s in SCENARIOS:
        if s.dialogue_id:
            assert s.dialogue_id in ids, f"scenario {s.id} -> {s.dialogue_id}"


def test_scripted_actions_name_only_real_tools_or_nothing():
    """A typo in a tool name would silently script a no-op."""
    # Derived, not hand-written: a hand-maintained copy of the contract is the thing
    # that drifts. web_search was added to the contract and this gate caught it.
    from errand.policy import CONTRACT_TOOLS

    allowed = set(CONTRACT_TOOLS) | {None}
    for s in SCENARIOS:
        flat = [a for spec in s.scripted for a in (spec if isinstance(spec, list) else [spec])]
        for action in flat:
            assert action["tool"] in allowed, f"{s.id}: {action['tool']}"


def test_scenarios_with_user_turns_have_scripted_actions():
    """Except the watcher scenarios, which fire with no user turn behind them."""
    for s in SCENARIOS:
        if s.needs == "watcher":
            continue
        assert s.scripted, f"{s.id} has turns but no scripted actions"


def test_restart_scenarios_name_a_valid_marker():
    for s in SCENARIOS:
        if s.restart_at is None:
            continue
        turn_part, index_part = s.restart_at.split(":")
        assert turn_part.isdigit() and index_part.isdigit()


def test_expected_is_never_empty():
    for s in SCENARIOS:
        assert s.expected, s.id
