"""Phase 5 gate, part 2: the CLI must not own any user-facing sentence.

`I3b` says the terminal sentence is a pure function of the token. That guarantee
evaporates the moment the CLI is allowed to write one, so this file enforces the
rule mechanically rather than by review.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from errand.outcomes import OUTCOMES, render_outcome

CLI = Path(__file__).resolve().parents[1] / "errand" / "cli.py"


def _cli_source() -> str:
    return CLI.read_text()


def _cli_string_literals() -> list[str]:
    tree = ast.parse(_cli_source())
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_cli_calls_render_outcome_and_never_reimplements_it():
    assert "render_outcome" in _cli_source()


@pytest.mark.parametrize("token", OUTCOMES)
def test_no_terminal_sentence_literal_appears_in_the_cli(token):
    """Every outcome's sentence must come from outcomes.py, not from a f-string here."""
    sentence = render_outcome(token)
    # compare a distinctive fragment so a coincidentally similar line does not trip it
    fragment = sentence[:40]
    for literal in _cli_string_literals():
        assert fragment not in literal, (
            f"cli.py contains the `{token}` sentence; it must call render_outcome"
        )


def test_cli_imports_render_outcome_from_outcomes():
    tree = ast.parse(_cli_source())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported |= {a.name for a in node.names}
    assert "render_outcome" in imported


def test_cli_refuses_real_mode_without_credentials(monkeypatch, capsys):
    """A demo believed to be live while running a script is worse than an error."""
    from errand.cli import main

    for name in ("ERRAND_LLM_BASE_URL", "ERRAND_LLM_KEY", "ERRAND_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)

    code = main(["--llm", "real"])
    assert code == 2
    err = capsys.readouterr().err
    assert "ERRAND_LLM_BASE_URL" in err


def test_cli_starts_and_quits(monkeypatch, capsys):
    from errand import cli

    monkeypatch.setattr("builtins.input", lambda *_: "/quit")
    with pytest.raises(SystemExit):
        cli.main(["--seed", "7"])


def test_memory_command_lists_admissible_rows(monkeypatch, capsys):
    from errand import cli

    lines = iter(["/memory", "/quit"])

    def fake_input(*_):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    with pytest.raises(SystemExit):
        cli.main(["--seed", "7"])
    out = capsys.readouterr().out
    assert "preferred_area" in out or "(nothing stored)" in out


def test_state_command_reports_the_fsm():
    from errand.cli import _command

    class FakeOrch:
        class session:
            state = type("S", (), {"value": "NEW"})()
            slots: dict = {}
            turns = 0
            tool_calls = 0
        memory = None
        server = None

    class FakeWatcher:
        def tick(self):
            return []

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        _command("/state", FakeOrch(), FakeWatcher(), None, None)
    assert "NEW" in buf.getvalue()
