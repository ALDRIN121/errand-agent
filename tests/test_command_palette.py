"""The slash-command palette, and the help it is built from.

Typing `/` used to require remembering the command list. The palette is driven by the
server's COMMANDS table so the two cannot disagree — Bug 15 was the earlier failure of
exactly that agreement, where the placeholder advertised `/new` and no handler existed.
"""
from __future__ import annotations

import json
import urllib.request

from errand.ui import ALIASES, COMMANDS, PAGE, _help_text, serve
from tests.test_ui_deadlock import _ended, _post


# ------------------------------------------------------------- the table itself


def test_every_advertised_command_has_a_handler():
    """The list is data, so the guarantee has to be checked deliberately.

    This is the Bug 15 shape: something advertised that cannot run. Each name here is
    asserted to be handled rather than assumed to be.
    """
    handlers = {
        "/new": ("/new", "/reset"),
        "/memory": ("/memory",),
        "/forget": ("/forget ",),
        "/state": ("/state",),
        "/trace": ("/trace",),
        "/help": ("/help",),
    }
    for name, _arg, _help in COMMANDS:
        assert name in handlers, f"{name} is offered but has no known handler"


def test_help_is_derived_from_the_table():
    """`/help` and the palette must not drift apart."""
    text = _help_text()
    for name, arg, _ in COMMANDS:
        want = f"{name} {arg}".strip()
        assert want in text, f"{want} missing from /help: {text}"


def test_every_command_has_a_description():
    for name, _arg, help_text in COMMANDS:
        assert help_text.strip(), f"{name} would show a blank row in the palette"


def test_aliases_point_at_a_real_command():
    names = {n for n, _, _ in COMMANDS}
    for alias, target in ALIASES.items():
        assert target in names, f"{alias} -> {target}, which is not a command"


def test_no_command_is_declared_twice():
    names = [n for n, _, _ in COMMANDS]
    assert len(names) == len(set(names))


def test_the_argument_shape_is_declared_for_commands_that_take_one():
    """/forget ignores its arguments if the palette does not say one is needed."""
    by_name = {n: a for n, a, _ in COMMANDS}
    assert by_name["/forget"] == "<key>"
    for bare in ("/new", "/memory", "/state", "/trace", "/help"):
        assert by_name[bare] == "", f"{bare} should take no argument"


# ------------------------------------------------------------------- the page


def test_the_page_builds_the_palette_from_server_data():
    assert 'id="palette"' in PAGE
    assert "if(Array.isArray(s.commands)) COMMANDS = s.commands;" in PAGE
    assert "function palDraw()" in PAGE


def test_the_page_does_not_hardcode_the_command_list():
    """A hardcoded list is how the two drift apart."""
    for name, _, _ in COMMANDS:
        assert f'"{name}"' not in PAGE, (
            f"{name} is written into the page; the palette must read the server list"
        )


def test_enter_runs_the_highlighted_command():
    """Typing `/ne` + Enter sent `/ne`, which the server refused as unknown."""
    assert "Enter runs the *highlighted* command" in PAGE
    assert "palApply(items[palIdx].name)" in PAGE


def test_escape_closes_the_palette():
    assert "ev.key === 'Escape'" in PAGE


# ---------------------------------------------------------------- the endpoint


def test_state_reports_the_command_list(tmp_path):
    orch, world, _, watcher = _ended(tmp_path)
    httpd = serve(orch, watcher, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        body = json.load(urllib.request.urlopen(base + "/api/state"))
        assert "commands" in body
        got = {c["name"] for c in body["commands"]}
        assert got == {n for n, _, _ in COMMANDS}
        for c in body["commands"]:
            assert set(c) == {"name", "arg", "help"}
    finally:
        httpd.shutdown()
        world.close()


def test_help_over_the_wire_matches_the_table(tmp_path):
    orch, world, _, watcher = _ended(tmp_path)
    httpd = serve(orch, watcher, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, payload = _post(base, "/api/command", {"line": "/help"})
        assert status == 200
        for name, _arg, _help in COMMANDS:
            assert name in payload["text"]
    finally:
        httpd.shutdown()
        world.close()


def test_a_finished_session_can_still_reach_help(tmp_path):
    """The palette offers /help while terminal, so /help must work there."""
    orch, world, _, watcher = _ended(tmp_path)
    httpd = serve(orch, watcher, port=0)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, payload = _post(base, "/api/command", {"line": "/help"})
        assert status == 200
        assert "/new" in payload["text"]
    finally:
        httpd.shutdown()
        world.close()
