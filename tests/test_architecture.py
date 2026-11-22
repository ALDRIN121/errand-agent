"""Architectural guarantees, checked by reading the code rather than trusting it.

The skill's point about "two genuinely independent blocks": not-offered, refused at
the gateway, and refused at the approval store all read the same persisted state, so
one bad transition defeats all three. The pair that is actually independent is

  * this file — a static import-graph check that `book`/`cancel`/`reschedule` are
    reached only through `execute_if_approved`; and
  * the runtime digest token check in `ApprovalStore.validate`.

Neither subsumes the other: the grep catches a new caller, the token check catches a
new code path through an existing caller. Both must be present.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "errand"
SIDE_EFFECTING = ("book", "cancel", "reschedule")


def _sources() -> dict[str, ast.Module]:
    out = {}
    for path in ROOT.rglob("*.py"):
        out[str(path.relative_to(ROOT))] = ast.parse(path.read_text())
    return out


def test_state_set_is_exactly_eleven():
    """Ten booking states plus RESEARCHING for the read-only web lookup."""
    from errand.policy import State

    assert len(State) == 11


def test_the_mock_is_the_only_place_side_effects_are_implemented():
    """`def book(` and friends may exist only on the tool server."""
    offenders = []
    for name, tree in _sources().items():
        if name.endswith("mock.py") or name.endswith("gateway.py"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in SIDE_EFFECTING:
                    offenders.append(f"{name}:{node.name}")
    assert not offenders, f"side-effecting implementations outside the mock: {offenders}"


def test_side_effecting_tools_are_called_only_from_the_orchestrator_path():
    """Every `.book(`/`.cancel(`/`.reschedule(` call site must be in orchestrator.py.

    The orchestrator routes them through `ApprovalStore.execute_if_approved`, which is
    the monopoly. A call site anywhere else is a second door.
    """
    allowed = {"orchestrator.py", "approval.py", "mock.py"}
    offenders = []
    for name, tree in _sources().items():
        if name.split("/")[-1] in allowed:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in SIDE_EFFECTING:
                    offenders.append(f"{name}:{node.lineno}:{node.func.attr}")
    assert not offenders, f"side effects called outside the sanctioned path: {offenders}"


def test_execute_if_approved_exists_exactly_once():
    """The monopoly function is singular, so there is one place to audit."""
    found = []
    for name, tree in _sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "execute_if_approved":
                found.append(name)
    assert found == ["approval.py"], found


def test_policy_is_the_only_module_deciding_legality():
    """`assert_tool_allowed` must be called from the gateway, not re-derived."""
    callers = []
    for name, tree in _sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "assert_tool_allowed":
                    callers.append(name)
    assert callers == ["tools/gateway.py"], callers


def test_outcomes_has_one_renderer():
    found = []
    for name, tree in _sources().items():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "render_outcome":
                found.append(name)
    assert found == ["outcomes.py"], found


def test_the_cli_does_not_bypass_the_orchestrator():
    """The CLI may call handle_turn and handle_event, nothing lower."""
    tree = ast.parse((ROOT / "cli.py").read_text())
    forbidden = {"book", "cancel", "reschedule", "hold_slot", "search_providers"}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in forbidden:
                bad.append(f"{node.lineno}:{node.func.attr}")
    assert not bad, f"cli.py calls tools directly: {bad}"


def test_no_module_reads_the_wall_clock_directly():
    """Every timestamp comes from the clock, or the evals are not deterministic."""
    offenders = []
    for name, tree in _sources().items():
        if name.split("/")[-1] == "clock.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("now", "utcnow", "time") and isinstance(
                    node.func.value, ast.Name
                ) and node.func.value.id in ("datetime", "time"):
                    offenders.append(f"{name}:{node.lineno}")
    assert not offenders, f"direct wall-clock reads: {offenders}"


def test_runtime_dependencies_stay_empty():
    """The design claims no third-party runtime deps; prove it."""
    import json

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("no pyproject")
    text = pyproject.read_text()
    if "dependencies = [" not in text:
        return
    block = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    deps = [line.strip().strip('",') for line in block.splitlines()
            if line.strip() and not line.strip().startswith("#")]
    assert deps == [] or deps == [""], f"runtime deps appeared: {deps}"


def test_llm_transport_is_stdlib_only():
    """HttpLLMClient exists so a real model needs no SDK."""
    tree = ast.parse((ROOT / "llm.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    third_party = {
        m for m in modules
        if m not in ("__future__", "json", "urllib", "ssl", "pathlib", "dataclasses",
                      "typing", "errand")
    }
    assert not third_party, f"llm.py imports {third_party}"
