"""Domain types. Frozen dataclasses and closed Literals only; no behaviour."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

ErrorClass = Literal[
    "transient", "not_found", "validation", "timeout",
    "hold_expired", "slot_taken", "slot_conflict", "unexpected",
]

BookingStatus = Literal["confirmed", "cancelled", "cancelled_by_provider"]
MemorySource = Literal["user", "inferred"]


class ToolError(Exception):
    """A tool failure with a class the orchestrator can branch on.

    Raw exceptions never escape the gateway: they are converted to this with
    error_class="unexpected" so no code path has to catch broadly.
    """

    def __init__(self, error_class: ErrorClass, detail: str = "") -> None:
        self.error_class = error_class
        self.detail = detail
        super().__init__(f"{error_class}: {detail}" if detail else error_class)


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime
    available: bool = True

    def key(self) -> str:
        return self.start.astimezone(tz=None).isoformat()


@dataclass(frozen=True)
class Provider:
    id: str
    seed_index: int
    service: str
    area: str
    price_paise: int
    rating: float
    attributes: dict = field(default_factory=dict)
    slots: list[Slot] = field(default_factory=list)


@dataclass(frozen=True)
class Review:
    author: str
    rating: int
    text: str
    trust: Literal["untrusted"] = "untrusted"


@dataclass(frozen=True)
class Hold:
    hold_id: str
    provider_id: str
    slot: Slot
    quote_paise: int
    expires_at: datetime


@dataclass
class Booking:
    booking_id: str
    user_id: str
    provider_id: str
    service: str
    area: str
    slot: Slot
    status: BookingStatus
    idempotency_key: str


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    args: dict
    result: object
    error: str | None
