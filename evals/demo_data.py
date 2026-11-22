"""Turn the demo fixture into a live, seeded environment.

The fixture stores day *offsets* rather than absolute dates, so the demo data stays
coherent whenever it is loaded instead of decaying into the past. This module is
the only place that converts offsets into real datetimes.
"""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from errand.clock import IST, Clock
from errand.db import Database
from errand.history import History
from errand.memory import MemoryStore
from errand.tools.mock import MockToolServer
from errand.types import Booking, Review, Slot

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "demo_data.json"
ANCHOR = datetime(2026, 11, 23, 0, 0, tzinfo=IST)  # a Monday


@dataclass
class DemoWorld:
    server: MockToolServer
    db: Database
    memory: MemoryStore
    history: History
    users: dict[str, dict]
    dialogues: list[dict]
    reviews: dict[str, list[Review]] = field(default_factory=dict)

    def provider_reviews(self, provider_id: str) -> list[Review]:
        """Fixture reviews if present, else the mock's deterministic default."""
        if provider_id in self.reviews:
            return self.reviews[provider_id]
        return self.server.get_reviews(provider_id)

    def bookings_for(self, user_id: str) -> list[Booking]:
        return self.server.list_bookings(user_id)

    def close(self) -> None:
        self.db.close()


def load_fixture(path: Path = FIXTURE) -> dict:
    return json.loads(Path(path).read_text())


def slot_at(day_offset: int, hour: int) -> Slot:
    start = ANCHOR + timedelta(days=day_offset, hours=hour)
    return Slot(start=start, end=start + timedelta(hours=1), available=True)


def build_world(
    clock: Clock,
    db_path: str | Path | None = None,
    seed: int = 7,
    scenario: str = "happy",
    user_id: str = "u_aldrin",
    fixture: dict | None = None,
) -> DemoWorld:
    """Build a world with prior bookings, stored memories and a long history.

    `db_path=None` creates a unique temp file, so two worlds in one process never
    share state. A caller passing a path gets a world it can reopen later, which is
    what the restart scenarios need.
    """
    data = fixture or load_fixture()
    if db_path is None:
        fd, path = tempfile.mkstemp(suffix=".db", prefix="errand_demo_")
        Path(path).unlink(missing_ok=True)
        import os

        os.close(fd)
        db_path = Path(path)

    db = Database(db_path)
    server = MockToolServer(clock=clock, seed=seed, scenario=scenario)
    history = History()
    memory = MemoryStore(db, clock, history=history)

    users = {u["id"]: u for u in data["users"]}

    # --- reviews from the fixture, typed as untrusted
    reviews: dict[str, list[Review]] = {}
    for pid, block in data.get("provider_reviews", {}).items():
        if pid.startswith("_"):
            continue
        reviews[pid] = [
            Review(author=r["author"], rating=r["rating"], text=r["text"])
            for r in block["reviews"]
        ]

    # --- prior bookings, with slots resolved from day offsets
    for row in data.get("bookings", []):
        booking = Booking(
            booking_id=row["booking_id"],
            user_id=row["user_id"],
            provider_id=row["provider_id"],
            service=row["service"],
            area=row["area"],
            slot=slot_at(row["slot_day_offset"], row["slot_hour"]),
            status=row["status"],
            idempotency_key=row["idempotency_key"],
        )
        server._by_id[booking.booking_id] = booking
        server._by_key[booking.idempotency_key] = booking

    # --- memories for the requested user
    user = users.get(user_id, {})
    if user:
        memory.add(user_id, "preferred_area", user["area"], "location", source="user")
        memory.add(user_id, "preferred_phone_hint", user["phone_hint"], "contact")
        memory.add(
            user_id, "budget_ceiling",
            str(user["default_budget_paise"] // 100), "budget", source="user",
        )
        memory.add(
            user_id, "earliest_acceptable_time",
            user["earliest_acceptable_time"], "scheduling", source="user",
        )
        memory.add(user_id, "preferred_language", user["preferred_language"], "communication")
        # "last X provider" means the most recent *confirmed* booking, not any
        # booking: writing one row per booking produced two conflicting
        # `last_electrician_provider` entries in the first version.
        latest: dict[str, dict] = {}
        for row in data.get("bookings", []):
            if row["user_id"] != user_id or row["status"] != "confirmed":
                continue
            service = row["service"]
            if service not in latest or row["slot_day_offset"] > latest[service]["slot_day_offset"]:
                latest[service] = row
        for service, row in sorted(latest.items()):
            memory.add(
                user_id, f"last_{service}_provider", row["provider_id"],
                "technician", source="user",
            )

    # --- rows that must be excluded from every context: a forgotten preference and
    # an unconfirmed inference. Seeded through the real API so the cascade applies.
    if user:
        stale_id = memory.add(user_id, "preferred_area_old", "Indiranagar", "location",
                              source="user")
        memory.forget(user_id, "preferred_area_old")
        memory.infer(user_id, "probable_budget", "2500", "budget")

    # --- the long prior conversation, with spans so forgetting can redact it
    convo = data.get("prior_conversation", {})
    if convo.get("user_id") == user_id:
        for turn in convo.get("turns", []):
            spans = {}
            if "area" in turn.get("states", []):
                spans["area"] = "Indiranagar"
            if "budget" in turn.get("states", []):
                spans["budget"] = "fifteen hundred"
            if "earliest_time" in turn.get("states", []):
                spans["earliest_time"] = "ten"
            history.append(turn["role"], turn["text"], spans=spans)

    return DemoWorld(
        server=server,
        db=db,
        memory=memory,
        history=history,
        users=users,
        dialogues=data.get("example_dialogues", []),
        reviews=reviews,
    )
