"""Conversation history that can be redacted.

The context budget replays the last N turns. That means a preference the user
stated out loud in turn 3 is inside the model's context on turn 9 — even after the
memory row for it is tombstoned. A tombstone alone therefore does not keep a
forgotten fact away from the model; this module is the part that does.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REDACTED = "[forgotten]"


@dataclass
class Turn:
    role: str
    text: str
    # memory id -> the exact substring of `text` that stated it
    spans: dict[str, str] = field(default_factory=dict)


class History:
    def __init__(self) -> None:
        self.turns: list[Turn] = []

    def append(self, role: str, text: str, spans: dict[str, str] | None = None) -> Turn:
        turn = Turn(role=role, text=text, spans=dict(spans or {}))
        self.turns.append(turn)
        return turn

    def forget(self, memory_id: str) -> int:
        """Replace every span that stated this memory with a marker.

        Returns the number of turns redacted. The turn keeps its shape so the
        conversation remains coherent; the value is gone.
        """
        hits = 0
        for turn in self.turns:
            value = turn.spans.pop(memory_id, None)
            if value:
                # replace every occurrence, and only this memory's value
                turn.text = turn.text.replace(value, REDACTED)
                hits += 1
        return hits

    def render(self, limit: int = 6) -> list[dict]:
        """The last `limit` turns, already redacted."""
        return [
            {"role": t.role, "text": t.text} for t in self.turns[-limit:]
        ]

    def render_text(self, limit: int = 6) -> str:
        return "\n".join(f"{t['role']}: {t['text']}" for t in self.render(limit))

    def __len__(self) -> int:
        return len(self.turns)
