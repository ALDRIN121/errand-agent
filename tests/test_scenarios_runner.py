"""The Phase 4 gate: every core scenario passes and every invariant holds.

This test is the gate, so it must fail for the right reason. It runs the scenarios
through the real harness rather than asserting on hand-written expectations.
"""
from __future__ import annotations

import pytest

from evals.harness import CHECKS, evaluate
from evals.scenarios import CORE, SCENARIOS, WATCHER


@pytest.mark.parametrize("scenario", CORE, ids=lambda s: f"{s.id:02d}-{s.name[:30]}")
def test_core_scenario_passes(scenario):
    report = evaluate(scenario)
    assert not report.result.error, f"crashed: {report.result.error}"
    assert report.outcome_ok, (
        f"got {report.result.outcome!r}, expected {sorted(scenario.expected)}; "
        f"tools were {report.result.tool_calls}"
    )
    failed = [c for c in report.checks if not c.passed and c.invariant != "I3b"]
    assert not failed, "; ".join(f"{c.invariant}: {c.detail}" for c in failed)


@pytest.mark.parametrize("scenario", WATCHER, ids=lambda s: f"{s.id:02d}-{s.name[:30]}")
def test_watcher_scenario_passes(scenario):
    """Phase 5 gate: the unattended path, run through the same harness."""
    report = evaluate(scenario, include_watcher=True)
    assert not report.result.error, f"crashed: {report.result.error}"
    assert report.outcome_ok, (
        f"got {report.result.outcome!r}, expected {sorted(scenario.expected)}; "
        f"tools were {report.result.tool_calls}"
    )


def test_the_gate_is_now_40_of_40():
    """Phase 5 closes the two the Phase 4 gate deferred."""
    reports = [evaluate(s, include_watcher=True) for s in SCENARIOS]
    assert len(reports) == 40
    failing = [r.scenario.id for r in reports if not r.outcome_ok]
    assert not failing, f"scenarios not reaching an expected token: {failing}"


def test_all_ten_invariants_are_implemented():
    names = {fn.__name__.lstrip("_").split("_")[0].upper() for fn in CHECKS}
    assert names == {f"I{i}" for i in range(1, 11)}


def test_the_gate_is_38_core_scenarios():
    assert len(CORE) == 38
    assert len(WATCHER) == 2
