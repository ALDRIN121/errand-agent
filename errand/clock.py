"""Time. Every timestamp in the codebase comes from here, never from datetime.now()."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

IST = timezone(timedelta(hours=5, minutes=30))


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class RealClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        import time

        return time.monotonic()


class FakeClock:
    """Deterministic clock for tests and evals.

    monotonic() advances only when the wall clock does, so a test that advances
    five minutes of booking time does not also trip a latency deadline.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 11, 23, 6, 0, tzinfo=IST)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
        self._mono += seconds

    def set(self, when: datetime) -> None:
        self._now = when
