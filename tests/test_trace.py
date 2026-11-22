"""The trace is the evidence base, so its own correctness needs tests.

Each of these caught a real defect: an append-mode writer duplicated every event, a
substring credential filter redacted the context-budget count, and a too-early read
of `outcome_verb` attributed a verified cancel to `book`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from errand.trace import Tracer, read_trace, redact, sanitize


def test_a_trace_path_holds_exactly_one_run(tmp_path):
    """Appending duplicated every event when a scenario was re-run."""
    path = tmp_path / "run.jsonl"
    with Tracer(path) as t:
        t.emit("turn", n=1)
    with Tracer(path) as t:
        t.emit("turn", n=1)
    assert len(read_trace(path)) == 1


def test_sequence_numbers_restart_per_run(tmp_path):
    path = tmp_path / "run.jsonl"
    with Tracer(path) as t:
        t.emit("a")
        t.emit("b")
    assert [e["seq"] for e in read_trace(path)] == [1, 2]


def test_credential_keys_are_redacted():
    out = redact({"api_key": "sk-1", "password": "hunter2", "authorization": "Bearer x"})
    assert out == {"api_key": "[redacted]", "password": "[redacted]",
                   "authorization": "[redacted]"}


def test_the_token_count_is_not_redacted():
    """`tokens` is the context-budget count and is the whole point of the manifest."""
    assert redact({"tokens": 806}) == {"tokens": 806}


def test_the_idempotency_key_is_not_redacted():
    """It is a deterministic public id the dedupe assertions read."""
    key = "b34fc687-d419-5319-93c1-2fda58acc338"
    assert redact({"idempotency_key": key})["idempotency_key"] == key


def test_control_characters_are_stripped():
    assert "\x00" not in sanitize("a\x00b")


def test_long_values_are_truncated_with_a_note():
    out = sanitize("x" * 5000)
    assert len(out) < 5000 and "chars" in out


def test_sanitize_never_raises_on_odd_input():
    class Weird:
        def __str__(self):
            return "weird"

    assert sanitize({"a": Weird(), "b": {1: "x"}}) is not None


def test_a_verified_success_emits_side_effect_ok(tmp_path):
    """The timeout-that-booked path claimed success with no record of it."""
    from errand.approval import ApprovalStore
    from errand.clock import FakeClock
    from errand.context import ContextAssembler
    from errand.llm import Action, ScriptedLLM
    from errand.orchestrator import Orchestrator
    from evals.demo_data import ANCHOR, build_world

    path = tmp_path / "t.jsonl"
    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, seed=7, scenario="timeout_booked", user_id="u_aldrin")
    tracer = Tracer(path)
    llm = ScriptedLLM([
        Action("hold_slot", {"provider_id": "prov_06",
                             "slot": world.server.get_provider("prov_06").slots[0]}),
        Action("book"),
        Action("get_booking_status"),
    ])
    orch = Orchestrator(
        clock=clock, server=world.server, db=world.db, memory=world.memory,
        history=world.history, approvals=ApprovalStore(world.db, clock), llm=llm,
        tracer=tracer, context=ContextAssembler(), user_id="u_aldrin",
    )
    orch.handle_turn("Book prov_06 tomorrow at 10.")
    orch.handle_turn("yes")
    tracer.close()
    events = read_trace(path)
    kinds = [e["type"] for e in events]
    assert "side_effect_ok" in kinds, kinds
    ok = next(e for e in events if e["type"] == "side_effect_ok")
    assert ok["tool"] == "book", "a verified book must be attributed to book"
    assert ok.get("verified") is True
    world.close()


def test_every_tool_call_has_a_preceding_action_attribution(tmp_path):
    """I8: without attribution a scripted suite passes against a switch statement."""
    from errand.approval import ApprovalStore
    from errand.clock import FakeClock
    from errand.context import ContextAssembler
    from errand.llm import Action, ScriptedLLM
    from errand.orchestrator import Orchestrator
    from evals.demo_data import ANCHOR, build_world

    path = tmp_path / "t.jsonl"
    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, seed=7, user_id="u_aldrin")
    tracer = Tracer(path)
    llm = ScriptedLLM([Action("search_providers", {
        "service": "plumber", "area": "Koramangala",
        "date_range": {"start": "2026-11-24", "end": "2026-11-29"},
    })])
    orch = Orchestrator(
        clock=clock, server=world.server, db=world.db, memory=world.memory,
        history=world.history, approvals=ApprovalStore(world.db, clock), llm=llm,
        tracer=tracer, context=ContextAssembler(), user_id="u_aldrin",
    )
    orch.handle_turn("plumber in Koramangala this week")
    tracer.close()
    events = read_trace(path)
    attributions = [e for e in events if e["type"] == "action_attributed"]
    calls = [e for e in events if e["type"] == "tool_call"]
    assert attributions, "no attribution emitted"
    assert len(attributions) >= len(calls)
    for call in calls:
        assert any(a["tool"] == call["name"] for a in attributions), call["name"]
    world.close()
