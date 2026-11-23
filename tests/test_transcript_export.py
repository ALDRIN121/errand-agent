"""The transcript exporter publishes to a public repo, so redaction is load-bearing.

The current session happens to contain no credential. That is luck, not a property, and
it does not survive the next session — so the writer is tested rather than trusted.
"""
from __future__ import annotations

import pathlib
import re

import pytest
from docs.export_transcript import REDACTED, redact

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("secret", [
    "sk-abcdefghijklmnopqrstuvwxyz123456",
    "sk-or-v1-abcdefghijklmnopqrstuvwxyz123456",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "xoxb-123456789012-abcdefghijkl",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
])
def test_credential_shapes_are_redacted(secret):
    out = redact(f"here is the value {secret} in a line")
    assert secret not in out
    assert REDACTED in out


@pytest.mark.parametrize("line", [
    "api_key = abcdefghijklmnop1234",
    "API_KEY: abcdefghijklmnop1234",
    "token=abcdefghijklmnop1234",
    "password: hunter2hunter2hunter2",
    "secret = 0123456789abcdef",
])
def test_labelled_secrets_are_redacted(line):
    out = redact(line)
    assert "abcdefghijklmnop1234" not in out
    assert "hunter2hunter2hunter2" not in out
    assert REDACTED in out


def test_ordinary_text_is_untouched():
    """Over-redaction would make the transcript useless to a reader."""
    text = "The `list_bookings` call failed because the prompt said no arguments."
    assert redact(text) == text


def test_short_identifiers_are_not_mistaken_for_secrets():
    text = "bk_1003 cost 1400 at prov_31 in Koramangala"
    assert redact(text) == text


def test_the_published_transcript_contains_no_secret():
    """The file that actually ships is scanned, not just the function that writes it."""
    for name in ("session.md", "session-2.md"):
        path = ROOT / "docs" / "ai-transcripts" / name
        if not path.exists():
            continue
        text = path.read_text()
        for pat in (
            r"\bsk-[A-Za-z0-9_\-]{16,}",
            r"\bgh[pousr]_[A-Za-z0-9]{20,}",
            r"\bAKIA[0-9A-Z]{16}\b",
        ):
            hits = re.findall(pat, text)
            assert not hits, f"{name} contains a credential-shaped string: {hits[:2]}"


def test_the_published_transcripts_are_tracked():
    """A transcript that is gitignored is not shared, whatever the README says."""
    import subprocess
    listed = subprocess.run(
        ["git", "ls-files", "docs/ai-transcripts/"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout
    for name in ("session.md", "session-2.md"):
        assert f"docs/ai-transcripts/{name}" in listed, f"{name} is not committed"
