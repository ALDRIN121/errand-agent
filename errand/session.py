"""Per-run state and the result of one turn.

Kept apart from the orchestrator so collaborators (execution, recovery) can name the
types without importing the object that drives them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from errand.policy import State


@dataclass
class TurnResult:
    state: State
    reply: str | None
    tool: str | None
    outcome: str | None = None
    sentence: str | None = None
    refused: str | None = None


@dataclass
class Session:
    id: str
    user_id: str
    state: State = State.NEW
    slots: dict = field(default_factory=dict)
    outcome: str | None = None
    outcome_verb: str | None = None
    turns: int = 0
    tool_calls: int = 0
    clarify_turns: int = 0
    recovery_declined: str | None = None
