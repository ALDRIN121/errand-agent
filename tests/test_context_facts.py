"""What the model is told about the request it is already in the middle of.

Three defects motivated this file, all reported from a live UI session:

1. The model asked "which Saturday?" because nothing in its context said what day it
   was. The clock knew; the prompt did not.
2. It asked "which booking?" for a booking the runtime had *already resolved* and was
   holding in `session.slots['manage']`. The fact existed in the database and in the
   state machine, but was never put in front of the model.
3. The UI's placeholder said "/new to start another" and no such command existed — a
   finished session could only be revived by restarting the process, which is why a
   "new" chat still arrived carrying the old conversation.
"""
from __future__ import annotations

from errand.approval import ApprovalStore
from errand.clock import FakeClock
from errand.context import ContextAssembler
from errand.history import History
from errand.llm import Action, ScriptedLLM
from errand.memory import MemoryStore
from errand.orchestrator import Orchestrator
from errand.policy import State
from errand.ui import serve
from evals.demo_data import ANCHOR, build_world

import json
import urllib.request


def _orch(actions, scenario="happy"):
    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, seed=7, scenario=scenario, user_id="u_aldrin")
    orch = Orchestrator(
        clock=clock, server=world.server, db=world.db, memory=world.memory,
        history=world.history, approvals=ApprovalStore(world.db, clock),
        llm=ScriptedLLM(actions), context=ContextAssembler(), user_id="u_aldrin",
    )
    return orch, world, clock


def _prompt(orch) -> str:
    prompt, _ = orch.context.assemble(
        state=orch.session.state, approval_kind=None, history=orch.history,
        memory=orch.memory, user_id=orch.session.user_id, intent="x",
        now=orch._today(), facts=orch._established_facts(),
    )
    return ContextAssembler().render_text(prompt)


# ------------------------------------------------------------------ the date


def test_the_prompt_says_what_day_it_is():
    """Without a date, "Saturday" is unanswerable and the model has to ask."""
    orch, world, _ = _orch([])
    try:
        text = _prompt(orch)
        assert "Today is Monday, 23 November 2026" in text
    finally:
        world.close()


def test_the_date_comes_from_the_clock_not_the_wall_clock():
    """An eval run must see the same instant as the fixture claims."""
    from datetime import datetime

    from errand.clock import IST

    orch, world, clock = _orch([])
    try:
        clock.set(datetime(2027, 3, 6, 9, 0, tzinfo=IST))
        assert "Today is Saturday, 06 March 2027" in _prompt(orch)
    finally:
        world.close()


def test_no_date_line_when_none_is_supplied():
    """The assembler stays usable without a clock: no argument, no invented date."""
    db = __import__("errand.db", fromlist=["Database"]).Database(":memory:")
    clock = FakeClock(ANCHOR)
    memory = MemoryStore(db, clock, history=History())
    prompt, _ = ContextAssembler().assemble(
        state=State.NEW, approval_kind=None, history=History(), memory=memory,
        user_id="u_aldrin", intent="hi",
    )
    assert "Today is" not in ContextAssembler().render_text(prompt)


# ------------------------------------------------------------- established facts


def test_a_resolved_booking_reaches_the_prompt():
    """The model must not re-ask for a booking the runtime already resolved."""
    orch, world, _ = _orch([Action("list_bookings", {}, "Looking up your booking.")])
    try:
        orch.handle_turn("Move my AC servicing to Saturday.")
        assert orch.session.slots.get("manage"), "the flow resolved a booking"
        booking = orch.session.slots["manage"]["booking"]

        text = _prompt(orch)
        assert "Established so far:" in text
        assert booking.booking_id in text
        assert booking.service in text
        assert booking.provider_id in text
    finally:
        world.close()


def test_the_manage_intent_reaches_the_prompt():
    orch, world, _ = _orch([Action("list_bookings", {}, "ok")])
    try:
        orch.handle_turn("Move my AC servicing to Saturday.")
        assert "the user wants to reschedule it" in _prompt(orch)
    finally:
        world.close()


def test_no_facts_block_on_a_cold_start():
    """A fresh session has nothing established, and must not claim otherwise."""
    orch, world, _ = _orch([])
    try:
        assert orch._established_facts() == []
        assert "Established so far:" not in _prompt(orch)
    finally:
        world.close()


def test_a_held_slot_is_stated_as_a_fact():
    orch, world, _ = _orch([])
    try:
        provider = world.server.search_providers("plumber", "Koramangala")[0]
        orch.hold = world.server.hold_slot(provider.id, provider.slots[0])
        facts = orch._established_facts()
        assert any("is held with" in f for f in facts), facts
    finally:
        world.close()


# ----------------------------------------------------------------------- reset


def _post(base: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        base + path, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    return json.load(urllib.request.urlopen(req))


def test_new_starts_a_fresh_conversation():
    """/new was advertised in the UI and did not exist."""
    orch, world, _ = _orch([Action("list_bookings", {}, "ok")])
    httpd = serve(orch, None, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        orch.handle_turn("Move my AC servicing to Saturday.")
        assert orch.session.state is State.SELECTING
        assert orch.session.slots
        assert len(orch.history) > 0

        out = _post(base, "/api/command", {"line": "/new"})
        assert "new conversation" in out["text"]

        assert orch.session.state is State.NEW
        assert orch.session.slots == {}
        assert len(orch.history) == 0
        assert orch.pending is None and orch.hold is None
    finally:
        httpd.shutdown()
        world.close()


def test_new_keeps_memory_and_bookings():
    """A new conversation is not a new user: preferences and bookings survive."""
    orch, world, _ = _orch([Action("list_bookings", {}, "ok")])
    httpd = serve(orch, None, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        memory_before = len(orch.memory.admissible("u_aldrin"))
        bookings_before = len(world.server.list_bookings("u_aldrin"))
        assert memory_before and bookings_before

        _post(base, "/api/command", {"line": "/new"})

        assert len(orch.memory.admissible("u_aldrin")) == memory_before
        assert len(world.server.list_bookings("u_aldrin")) == bookings_before
    finally:
        httpd.shutdown()
        world.close()


def test_a_reset_session_keeps_the_same_user():
    """Reset must not silently switch which customer the session belongs to."""
    orch, world, _ = _orch([])
    try:
        assert orch.session.user_id == "u_aldrin"
        orch.reset()
        assert orch.session.user_id == "u_aldrin"
        assert orch.gateway.user_id == "u_aldrin"
    finally:
        world.close()


def test_new_appears_in_help():
    orch, world, _ = _orch([])
    httpd = serve(orch, None, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        assert "/new" in _post(base, "/api/command", {"line": "/help"})["text"]
    finally:
        httpd.shutdown()
        world.close()
