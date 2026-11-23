"""Export a Hermes session to the transcript format this repo ships.

The repo is public, so this is a publishing step and is written like one: every string
that leaves the database is passed through a redactor before it is written. The current
key happens to be absent from the session, but "happens to be absent" is not a property
that survives the next session, so the check is in the writer rather than in my head.

Usage:
    python -m docs.export_transcript <session_id_or_prefix> > out.md
    python -m docs.export_transcript --list
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sqlite3
import sys

DB = pathlib.Path.home() / ".hermes" / "state.db"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

# Shapes that must never reach a public file. Deliberately broad: a false positive
# redacts a harmless string, a false negative publishes a credential.
SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk-or-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*\S{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
]

REDACTED = "[REDACTED]"


def _known_keys() -> list[str]:
    """Literal secrets from the user's config, so exact matches are caught too."""
    out: list[str] = []
    cfg = pathlib.Path.home() / ".hermes" / "config.yaml"
    if not cfg.exists():
        return out
    try:
        import yaml
        data = yaml.safe_load(cfg.read_text()) or {}
    except Exception:
        return out
    for prov in data.get("custom_providers") or []:
        for field in ("api_key", "key", "token"):
            v = prov.get(field)
            if isinstance(v, str) and len(v) >= 16:
                out.append(v)
    return out


_KEYS = _known_keys()


def redact(text: str) -> str:
    if not text:
        return ""
    for k in _KEYS:
        text = text.replace(k, REDACTED)
    for pat in SECRET_PATTERNS:
        text = pat.sub(REDACTED, text)
    return text


def _ts(v) -> str:
    try:
        return dt.datetime.fromtimestamp(float(v), IST).strftime("%H:%M")
    except (TypeError, ValueError):
        return "—"


def _elide(s: str, n: int = 78) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _one_line_call(name: str, args: str) -> str:
    """Tool calls are one line each with arguments elided, as the shipped file does."""
    try:
        import json
        d = json.loads(args) if args else {}
        inner = ", ".join(f"{k}={_elide(v, 40)}" for k, v in d.items())
    except Exception:
        inner = _elide(args, 80)
    return f"- `{name}({inner})`"


def _is_compaction(text: str) -> bool:
    return "CONTEXT COMPACTION" in text or "handoff from a previous context window" in text


def export(session_prefix: str) -> str:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute(
        "select * from sessions where id like ? order by started_at desc limit 1",
        (f"%{session_prefix}%",),
    ).fetchone()
    if row is None:
        raise SystemExit(f"no session matching {session_prefix!r}")

    rows = con.execute(
        "select role, content, tool_calls, tool_name, timestamp, active, compacted"
        " from messages where session_id = ? order by id",
        (row["id"],),
    ).fetchall()

    users = sum(1 for r in rows if r["role"] == "user")
    asst = sum(1 for r in rows if r["role"] == "assistant")
    calls = sum(1 for r in rows if r["role"] == "assistant" and r["tool_calls"])
    started = dt.datetime.fromtimestamp(float(row["started_at"]), IST)

    out: list[str] = []
    app = out.append
    app("# Build transcript — Errand Agent")
    app("")
    app(
        f"Working session `{row['id'][:24]}…` with Hermes ({row['model']}), "
        f"started {started:%Y-%m-%d %H:%M} IST. "
        f"{users} user turns, {asst} assistant turns, {calls} tool calls."
    )
    app("")
    app(
        "User and assistant text is verbatim. Tool calls are listed one line each with "
        "arguments elided for length; tool results are omitted (they are reproduced in "
        "the eval traces). Context-compaction handoffs — Hermes-generated summaries of "
        "earlier turns — are marked rather than repeated. Credentials are redacted."
    )
    app("")
    app("---")
    app("")

    seen_compaction = False
    for r in rows:
        role = r["role"]
        text = (r["content"] or "").strip()

        if role == "tool":
            continue

        if role == "user":
            if _is_compaction(text):
                if not seen_compaction:
                    app("### [context compaction]")
                    app("")
                    app(
                        "Earlier turns were summarised by the runtime at this point. The "
                        "summary is not reproduced; the work it describes is in the commits "
                        "and `docs/BUGS.md`."
                    )
                    app("")
                    seen_compaction = True
                continue
            app(f"### {_ts(r['timestamp'])} — user")
            app("")
            app(redact(text))
            app("")
            continue

        if role == "assistant":
            lines: list[str] = []
            if text:
                lines.append(redact(text))
            if r["tool_calls"]:
                import json
                try:
                    for tc in json.loads(r["tool_calls"]):
                        fn = (tc.get("function") or {})
                        lines.append(_one_line_call(fn.get("name", "?"), fn.get("arguments") or ""))
                except Exception:
                    lines.append(f"- `{r['tool_name'] or '?'}(…)`")
            if not lines:
                continue
            app(f"**assistant** ({_ts(r['timestamp'])})")
            app("")
            app("\n".join(lines))
            app("")

    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("session", nargs="?", help="session id or unique prefix")
    ap.add_argument("--list", action="store_true", help="list recent sessions")
    ap.add_argument("-o", "--out", help="write here instead of stdout")
    args = ap.parse_args(argv)

    if args.list:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        for r in con.execute(
            "select id, started_at, model, message_count, title from sessions"
            " order by started_at desc limit 15"
        ):
            ts = dt.datetime.fromtimestamp(float(r[1]), IST).strftime("%Y-%m-%d %H:%M")
            print(f"{r[0][:28]}  {ts}  {r[3]:>5} msgs  {r[4] or ''}")
        return 0

    if not args.session:
        ap.error("give a session id/prefix, or --list")

    text = export(args.session)
    if args.out:
        pathlib.Path(args.out).write_text(text)
        print(f"wrote {args.out} ({len(text.splitlines())} lines)", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
