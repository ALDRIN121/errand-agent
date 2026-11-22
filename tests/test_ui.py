"""The UI routes, driven over real HTTP against a real orchestrator.

No browser and no mocking of the server: bind port 0, POST to it, read the JSON that
comes back. A screenshot cannot prove `/api/state` reports the same state the
orchestrator holds; this can.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from errand.approval import ApprovalStore
from errand.clock import FakeClock
from errand.context import ContextAssembler
from errand.llm import ScriptedLLM
from errand.orchestrator import Orchestrator
from errand.trace import Tracer
from errand.ui import PAGE, serve, snapshot
from errand.watcher import Watcher
from evals.demo_data import ANCHOR, build_world
from evals.runner import actions_for
from evals.scenarios import SCENARIOS


def _ui(tmp_path, scenario_id: int = 1):
    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, user_id="u_aldrin")
    chosen = next(s for s in SCENARIOS if s.id == scenario_id)
    trace = Tracer(tmp_path / "trace.jsonl")
    orch = Orchestrator(
        clock=clock, server=world.server, db=world.db, memory=world.memory,
        history=world.history, approvals=ApprovalStore(world.db, clock),
        llm=ScriptedLLM(actions_for(chosen)), tracer=trace,
        context=ContextAssembler(), user_id="u_aldrin",
    )
    watcher = Watcher(server=world.server, orchestrator=orch, db=world.db,
                      clock=clock, tracer=trace)
    httpd = serve(orch, watcher, trace_path=trace.path, model_label="test",
                  port=0)
    return httpd, orch, watcher, trace, world


def _get(httpd, path):
    host, port = httpd.server_address[:2]
    with urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=20) as r:
        body = r.read()
        ctype = r.headers.get("Content-Type", "")
    return body, ctype


def _post(httpd, path, payload):
    host, port = httpd.server_address[:2]
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# ----------------------------------------------------------------- the page


def test_the_page_is_served_and_is_self_contained():
    """No CDN, no build step: the page must work from a bare checkout."""
    assert "<!doctype html>" in PAGE.lower()
    for remote in ("http://", "https://", "//cdn", "unpkg", "jsdelivr"):
        assert remote not in PAGE, f"page reaches out to {remote}"


def test_the_page_calls_only_our_own_endpoints():
    for endpoint in ("/api/state", "/api/turn", "/api/command"):
        assert endpoint in PAGE


def test_the_page_has_no_inline_credentials():
    lowered = PAGE.lower()
    for word in ("api_key", "bearer ", "sk-", "authorization"):
        assert word not in lowered


# ---------------------------------------------------------------- the state


def test_state_reports_what_the_orchestrator_holds(tmp_path):
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        body, ctype = _get(httpd, "/api/state")
        assert "application/json" in ctype
        payload = json.loads(body)
        assert payload["state"] == orch.session.state.value
        assert payload["turns"] == orch.session.turns
        assert payload["tool_calls"] == orch.session.tool_calls
        assert payload["model"] == "test"
        assert payload["budgets"]["turns"] > 0
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_state_shows_nothing_pending_before_a_hold(tmp_path):
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        payload = json.loads(_get(httpd, "/api/state")[0])
        assert payload["pending"] is None
        assert payload["terminal"] is False
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_a_turn_moves_the_state_the_ui_reports(tmp_path):
    """The page must not show a stale state after a turn."""
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        before = json.loads(_get(httpd, "/api/state")[0])["state"]
        status, result = _post(httpd, "/api/turn",
                               {"text": "Find a plumber in Koramangala this week"})
        assert status == 200
        after = json.loads(_get(httpd, "/api/state")[0])["state"]
        assert after != before or orch.session.turns == 1
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_an_empty_turn_is_refused(tmp_path):
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        status, payload = _post(httpd, "/api/turn", {"text": "   "})
        assert status == 400
        assert "error" in payload
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_a_turn_after_terminal_is_refused_not_crashed(tmp_path):
    """A finished session must say so, not throw a traceback at the page."""
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        orch._terminal("abandoned_by_user")
        status, payload = _post(httpd, "/api/turn", {"text": "hello"})
        assert status == 409
        assert "ended" in payload["error"].lower()
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


# ------------------------------------------------------------------ commands


def test_the_commands_mirror_the_cli(tmp_path):
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        for line, expect in [
            ("/state", "state:"),
            ("/memory", "preferred_area"),   # the demo world seeds this
            ("/help", "commands:"),
            ("/trace", str(trace.path)),
        ]:
            status, payload = _post(httpd, "/api/command", {"line": line})
            assert status == 200, line
            assert expect in payload["text"], (line, payload)
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_forget_through_the_ui_actually_forgets(tmp_path):
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        orch.memory.add("u_aldrin", "temp_probe", "Koramangala",
                        category="area", source="user")
        payload = json.loads(_get(httpd, "/api/state")[0])
        assert any(m["key"] == "temp_probe" for m in payload["memory"])

        status, result = _post(httpd, "/api/command", {"line": "/forget temp_probe"})
        assert status == 200
        assert "forgot" in result["text"]

        after = json.loads(_get(httpd, "/api/state")[0])
        assert not any(m["key"] == "temp_probe" for m in after["memory"])
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_an_unknown_command_says_so(tmp_path):
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        status, payload = _post(httpd, "/api/command", {"line": "/nonsense"})
        assert status == 200
        assert "unknown" in payload["text"]
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


# --------------------------------------------------------------------- trace


def test_the_trace_endpoint_serves_the_real_file(tmp_path):
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        _post(httpd, "/api/turn", {"text": "Find a plumber in Koramangala this week"})
        body, ctype = _get(httpd, "/api/trace")
        assert "ndjson" in ctype
        first = json.loads(body.decode().splitlines()[0])
        assert "seq" in first and "type" in first
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_the_events_panel_is_built_from_the_trace(tmp_path):
    """The panel must reflect the same events the harness asserts on."""
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        _post(httpd, "/api/turn", {"text": "Find a plumber in Koramangala this week"})
        payload = json.loads(_get(httpd, "/api/state")[0])
        assert payload["events"], "no events after a turn"
        assert all("label" in e and "seq" in e for e in payload["events"])
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


# ------------------------------------------------------- the architectural rule


def test_the_ui_never_calls_a_tool_directly():
    """`errand/ui.py` must reach tools only through the orchestrator.

    This is the same check `test_architecture.py` makes of the CLI, and it matters more
    here: a handler that could call `server.book` would be the second door.
    """
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[1] / "errand" / "ui.py").read_text())
    forbidden = {"book", "cancel", "reschedule", "hold_slot", "search_providers",
                 "get_provider", "list_bookings", "execute_if_approved"}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in forbidden:
                bad.append(f"{node.lineno}:{node.func.attr}")
    assert not bad, f"ui.py calls tools directly: {bad}"


def test_the_ui_does_not_render_terminal_sentences():
    """I3b: only `outcomes.render_outcome` may write a final sentence."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "errand" / "ui.py").read_text()
    for literal in ("Booked:", "Cancelled.", "Ref ", "nothing was booked"):
        assert literal not in text, f"ui.py writes a terminal sentence: {literal!r}"


def test_snapshot_carries_no_secret(tmp_path):
    """Whatever the page can read, the API key must not be in it."""
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        body = _get(httpd, "/api/state")[0].decode()
        assert "api_key" not in body.lower()
        assert "bearer" not in body.lower()
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


# ------------------------------------------- a finished session must not look live


def test_a_terminal_session_offers_no_approval(tmp_path):
    """Regression: the UI kept a Yes button for an already-booked slot.

    `orch.pending` deliberately survives `_terminal`, because the trace path reads it
    to attribute the outcome. A long-lived surface must therefore apply finished-ness
    itself, or it invites the user to approve something that already happened.
    """
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        _post(httpd, "/api/turn", {"text": "Find a plumber in Koramangala this week"})
        orch._terminal("booked")
        payload = json.loads(_get(httpd, "/api/state")[0])
        assert payload["terminal"] is True
        assert payload["pending"] is None, "a finished session showed a live approval"
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_clearing_the_approval_does_not_break_outcome_attribution():
    """`pending` must still be readable after `_terminal` for the trace path."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "errand" / "orchestrator.py").read_text()
    body = source.split("def _terminal", 1)[1].split("def ", 1)[0]
    assert "self.pending = None" not in body, (
        "_terminal must not clear pending: the verify path reads it afterwards"
    )


# -------------------------------------------- the status line must actually be live


def test_state_reads_do_not_queue_behind_a_running_turn(tmp_path):
    """Bug: /api/state took the same lock as /api/turn.

    A turn runs for the length of a model call, so the only endpoint that can say what
    is happening was blocked exactly while something was happening.
    """
    import threading
    import time

    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        # hold the turn lock, as a long model call would
        httpd.RequestHandlerClass.lock.acquire()
        try:
            started = time.monotonic()
            body = _get(httpd, "/api/state")[0]     # must not block
            elapsed = time.monotonic() - started
            assert elapsed < 2.0, f"/api/state waited {elapsed:.1f}s behind the turn"
            assert json.loads(body)["state"] == orch.session.state.value
        finally:
            httpd.RequestHandlerClass.lock.release()
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_a_turn_still_serialises_writes(tmp_path):
    """Reads may be concurrent; two turns still must not interleave."""
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        results: list[int] = []
        def fire(text):
            status, _ = _post(httpd, "/api/turn", {"text": text})
            results.append(status)
        threads = [__import__("threading").Thread(target=fire, args=(f"hello {i}",))
                   for i in range(3)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=20)
        assert all(status == 200 for status in results), results
        assert orch.session.turns == 3
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_progress_is_exposed_from_the_trace(tmp_path):
    """The page's status line is the trace's `progress` event, not a second channel."""
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        _post(httpd, "/api/turn", {"text": "Find a plumber in Koramangala this week"})
        payload = json.loads(_get(httpd, "/api/state")[0])
        assert payload["progress"] is not None, "no progress event reached the page"
        assert payload["progress"]["text"], payload["progress"]
        assert payload["progress"]["state"], payload["progress"]
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()


def test_polling_during_a_turn_returns_a_consistent_snapshot(tmp_path):
    """Snapshot copies what it reads; a mid-turn poll must not raise."""
    httpd, orch, watcher, trace, world = _ui(tmp_path)
    try:
        errors: list[str] = []
        import threading, time
        stop = threading.Event()
        def poll():
            while not stop.is_set():
                try:
                    json.loads(_get(httpd, "/api/state")[0])
                except Exception as exc:      # noqa: BLE001 - the assertion is below
                    errors.append(f"{type(exc).__name__}: {exc}")
                time.sleep(0.05)
        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        _post(httpd, "/api/turn", {"text": "Find a plumber in Koramangala this week"})
        stop.set(); poller.join(timeout=5)
        assert not errors, errors[:3]
    finally:
        httpd.shutdown(); httpd.server_close(); trace.close(); world.close()
