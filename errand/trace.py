"""Trace: one JSONL file per run.

Every fact an eval asserts on comes from here, so the writer is deliberately
boring: append-only, monotonically sequenced, sanitised on the way in.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_TRUNCATE = 2000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# Deliberately specific. A substring match on "token" redacted the context-budget
# *count* under the key `tokens`, which is exactly the evidence the manifest exists
# to record. `idempotency_key` is a deterministic public identifier the dedupe
# assertion reads, so it is not a secret either.
_SECRET = re.compile(
    r"(?i)^(api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|"
    r"password|passwd|authorization|auth[_-]?header|bearer|cookie|session[_-]?token|"
    r"access[_-]?token|refresh[_-]?token)$"
)


def sanitize(value: Any, truncated: bool = True) -> Any:
    """Strip control characters and cap length. Never raises on odd input."""
    if isinstance(value, str):
        text = _CONTROL.sub(" ", value)
        if truncated and len(text) > _TRUNCATE:
            return text[:_TRUNCATE] + f"…(+{len(text) - _TRUNCATE} chars)"
        return text
    if isinstance(value, dict):
        return {str(k): sanitize(v, truncated) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, truncated) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize(str(value), truncated)


def redact(value: Any) -> Any:
    """Mask anything whose key looks like a credential."""
    if isinstance(value, dict):
        return {
            k: ("[redacted]" if _SECRET.search(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class Tracer:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        # One trace file is one run. Appending silently duplicated every event when a
        # scenario was re-run, which made the excerpt unusable.
        self._fh = self.path.open("w", encoding="utf-8")

    def emit(self, type: str, **fields: Any) -> dict:
        self._seq += 1
        record = {"seq": self._seq, "type": type, **sanitize(redact(fields))}
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()
        return record

    def close(self) -> None:
        try:
            self._fh.close()
        except ValueError:
            pass

    def __enter__(self) -> "Tracer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def read_trace(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
