"""Ask once, carry the answers, then stop.

A clarification that names no options hands the work straight back to the user, and a
second question after no answer is an interrogation. Both were real: "Which provider do
you mean?" arrived with no providers listed, and an unanswerable area produced four
questions in a row.
"""

import datetime

from errand.clock import FakeClock
from errand.orchestrator import MAX_CLARIFY_TURNS, Orchestrator


def _orch():
    """A real orchestrator, assembled exactly as the runner assembles one."""
    from errand.approval import ApprovalStore
    from errand.context import ContextAssembler
    from errand.llm import ScriptedLLM
    from evals.runner import build_world

    clock = FakeClock(datetime.datetime(2026, 11, 23, 8, 0))
    world = build_world(clock, seed=7, scenario="happy", user_id="u_aldrin")
    return Orchestrator(
        clock=clock, server=world.server, db=world.db, memory=world.memory,
        history=world.history, approvals=ApprovalStore(world.db, clock),
        llm=ScriptedLLM([]), tracer=None, context=ContextAssembler(),
        user_id="u_aldrin",
    )


def test_the_agent_asks_once():
    assert MAX_CLARIFY_TURNS == 1


def test_a_question_about_providers_names_them():
    """An unanswerable question is worse than no question."""
    msg = _orch()._ask_which(service="plumber", area="Koramangala")
    assert "prov_06" in msg
    assert "Koramangala" in msg
    # it asks, and the list follows the ask rather than replacing it
    assert "?" in msg.split("\n")[0]
    assert msg.index("?") < msg.index("prov_06")


def test_a_question_about_a_service_with_nothing_to_offer_names_what_exists():
    msg = _orch()._ask_which(service="plumber", area="Jayanagar")
    assert "HSR Layout" in msg or "Indiranagar" in msg


def test_the_unknown_area_question_carries_the_areas():
    import pathlib
    import tempfile

    from evals.runner import run_scenario
    from evals.scenarios import SCENARIOS
    scenario = [s for s in SCENARIOS if s.id == 43][0]
    directory = pathlib.Path(tempfile.mkdtemp())
    run_scenario(scenario, trace_dir=directory)
    trace = (directory / "scenario_43.jsonl").read_text()
    assert '"type": "clarify"' in trace
    for area in ("Koramangala", "Indiranagar", "HSR Layout"):
        assert area in trace, area


def test_the_stop_sentence_names_what_was_missing():
    """'Some details are missing' is true and useless."""
    import pathlib
    import tempfile

    from evals.runner import run_scenario
    from evals.scenarios import SCENARIOS
    scenario = [s for s in SCENARIOS if s.id == 43][0]
    result = run_scenario(scenario, trace_dir=pathlib.Path(tempfile.mkdtemp()))
    assert result.outcome == "aborted_missing_details"
    assert "asked once" in (result.sentence or "").lower()
    # no schema names leak into a sentence a person reads
    for field in ("window_start", "window_end"):
        assert field not in (result.sentence or "")
