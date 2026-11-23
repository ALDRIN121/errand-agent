"""A finished session must not be a dead end.

Reported live: "Cannot even type a message now." The session had reached TERMINAL,
and the page disabled the text input on exactly that condition — while its
placeholder told the user to type `/new`. The one command that could revive the
session was the one command the UI made untypable, and a reload was the only way out.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from errand.approval import ApprovalStore
from errand.clock import FakeClock
from errand.context import ContextAssembler
from errand.llm import Action, ScriptedLLM
from errand.orchestrator import Orchestrator
from errand.policy import State
from errand.ui import PAGE, serve
from evals.demo_data import ANCHOR, build_world


def _ended(tmp_path):
    """An orchestrator sitting in TERMINAL, as the user found it."""
    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, seed=7, scenario="happy", user_id="u_aldrin")
    orch = Orchestrator(
        clock=clock, server=world.server, db=world.db, memory=world.memory,
        history=world.history, approvals=ApprovalStore(world.db, clock),
        llm=ScriptedLLM([Action(None, {}, "All done.")]),
        context=ContextAssembler(), user_id="u_aldrin",
        tracer=__import__("errand.trace", fromlist=["Tracer"]).Tracer(
            tmp_path / "t.jsonl"),
    )
    orch._terminal("abandoned_by_user")
    assert orch.session.state is State.TERMINAL
    watcher = __import__("errand.watcher", fromlist=["Watcher"]).Watcher(
        server=world.server, orchestrator=orch, db=world.db, clock=clock, tracer=None)
    return orch, world, tmp_path / "t.jsonl", watcher


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


# ------------------------------------------------------------------ the page


def test_the_input_is_not_disabled_for_a_finished_session():
    """The defect itself: `disabled` on TERMINAL made /new unreachable."""
    assert "disabled = !!s.terminal" not in PAGE, (
        "the page must not disable the input for a terminal session — a disabled "
        "input cannot accept the /new that revives it"
    )
    assert "disabled = busy" in PAGE


def test_the_page_offers_a_visible_way_to_start_again():
    assert 'id="newchat"' in PAGE
    assert "New chat" in PAGE


def test_a_reset_clears_the_visible_transcript():
    """A cleared session must not keep showing the old conversation."""
    assert "if(res.reset) { $('log').innerHTML = ''; rendered = 0; }" in PAGE


# ---------------------------------------------------------------- the routes


def test_a_turn_after_terminal_is_refused_with_a_usable_hint(tmp_path):
    orch, world, _, watcher = _ended(tmp_path)
    httpd = serve(orch, watcher, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, payload = _post(base, "/api/turn", {"text": "hello"})
        assert status == 409
        assert "/new" in payload["error"]
    finally:
        httpd.shutdown()
        world.close()


def test_new_is_accepted_from_a_terminal_session(tmp_path):
    """The whole point: the escape hatch works from the state that needs it."""
    orch, world, _, watcher = _ended(tmp_path)
    httpd = serve(orch, watcher, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, payload = _post(base, "/api/command", {"line": "/new"})
        assert status == 200, payload
        assert payload.get("reset") is True
        assert orch.session.state is State.NEW
    finally:
        httpd.shutdown()
        world.close()


def test_the_session_can_be_used_again_after_new(tmp_path):
    """Not just a state flag: a turn must actually work afterwards."""
    orch, world, _, watcher = _ended(tmp_path)
    httpd = serve(orch, watcher, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        _post(base, "/api/command", {"line": "/new"})
        status, payload = _post(base, "/api/turn", {"text": "hello again"})
        assert status == 200, payload
        assert payload.get("state") != "TERMINAL"
    finally:
        httpd.shutdown()
        world.close()


def test_the_transcript_survives_a_reload():
    """A reload must show the conversation the agent is still acting on.

    Reported: the page came back empty while the server still held a plumbing request,
    so "hi" was answered with a plumber pitch for a request the user could not see.
    """
    assert '"messages"' in PAGE or "messages" in PAGE
    assert "restore(s.messages)" in PAGE
    assert "function restore(msgs)" in PAGE


def test_the_state_endpoint_reports_the_transcript(tmp_path):
    orch, world, _, watcher = _ended(tmp_path)
    httpd = serve(orch, watcher, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        _post(base, "/api/command", {"line": "/new"})
        _post(base, "/api/turn", {"text": "Find a plumber in Koramangala"})
        body = json.load(urllib.request.urlopen(base + "/api/state"))
        assert "messages" in body, sorted(body)
        assert [m["role"] for m in body["messages"]][:2] == ["user", "assistant"]
        assert "plumber" in body["messages"][0]["text"].lower()
    finally:
        httpd.shutdown()
        world.close()


def test_the_transcript_is_empty_after_a_reset(tmp_path):
    orch, world, _, watcher = _ended(tmp_path)
    httpd = serve(orch, watcher, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        _post(base, "/api/command", {"line": "/new"})
        _post(base, "/api/turn", {"text": "Find a plumber in Koramangala"})
        _post(base, "/api/command", {"line": "/new"})
        body = json.load(urllib.request.urlopen(base + "/api/state"))
        assert body["messages"] == [], (
            f"a new conversation still shows {len(body['messages'])} old turns"
        )
    finally:
        httpd.shutdown()
        world.close()


def test_the_trace_panel_drops_the_previous_conversation(tmp_path):
    """A new chat whose trace still shows the old one reads as the old chat."""
    orch, world, trace, watcher = _ended(tmp_path)
    httpd = serve(orch, watcher, port=0, trace_path=trace)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        before = json.load(urllib.request.urlopen(base + "/api/state"))["events"]
        assert before, "the ended session left events behind"

        _post(base, "/api/command", {"line": "/new"})

        after = json.load(urllib.request.urlopen(base + "/api/state"))["events"]
        assert [e["type"] for e in after] == ["session_reset"], (
            f"expected only the reset marker, got {[e['type'] for e in after]}"
        )
    finally:
        httpd.shutdown()
        world.close()
