"""The mock provider environment.

Every failure behaviour the assignment fixes lives here, in one class, with a
seeded RNG and a fake clock so runs are reproducible. Branch order inside `book`
matters more than it looks: expiry must be checked before dedupe, or a stale hold
could return a booking, and dedupe must be checked before the random draw, or a
retry would roll the dice again instead of returning what already happened.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from errand.clock import IST, Clock
from errand.tools import payloads
from errand.types import (
    Booking,
    Hold,
    Provider,
    Review,
    Slot,
    ToolCallRecord,
    ToolError,
)

FIXTURE = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "providers.json"

SCENARIOS = {
    "happy",
    "slot_taken",
    "timeout_booked",
    "timeout_not_booked",
    "double_unknown",
    "price_shift",
    "cancel_transient",
    "reschedule_conflict",
    "reschedule_ok",
    "provider_cancels",
    "recover_unavailable",
}

# hours that always conflict on reschedule, per the contract's "configured slots"
CONFLICT_HOURS = {9, 14}


class MockToolServer:
    """In-process mock of the eight provider tools."""

    def __init__(self, clock: Clock, seed: int = 7, scenario: str = "happy",
                 web: object | None = None) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}")
        self.clock = clock
        self.seed = seed
        self.scenario = scenario
        self.web = web
        self._rng = random.Random(seed)

        self._providers: dict[str, Provider] = {}
        self._holds: dict[str, Hold] = {}
        self._by_key: dict[str, Booking] = {}
        self._by_id: dict[str, Booking] = {}
        self._cancel_attempts: dict[str, int] = {}
        self._hold_seq = 0
        self._booking_seq = 0

        self.calls: list[ToolCallRecord] = []
        self.events: list[dict] = []

        self._load_fixture()

    # ------------------------------------------------------------------ setup

    def _load_fixture(self) -> None:
        raw = json.loads(FIXTURE.read_text())
        for row in raw:
            slots = [
                Slot(
                    start=datetime(2026, 11, 23 + day, hour, 0, tzinfo=IST),
                    end=datetime(2026, 11, 23 + day, hour + 1, 0, tzinfo=IST),
                    available=True,
                )
                # A full week, not three days. The contract says the mock covers the
                # next seven days, and the demo's own headline request — "move my AC
                # servicing to *Saturday*" — was unanswerable because no Saturday
                # existed in the data: the model asked for a date, was told 28 Nov,
                # searched, and correctly found nothing.
                for day in range(7)
                for hour in row["slot_hours"]
            ]
            self._providers[row["id"]] = Provider(
                id=row["id"],
                seed_index=row["seed_index"],
                service=row["service"],
                area=row["area"],
                price_paise=row["price_paise"],
                rating=row["rating"],
                attributes=row["attributes"],
                slots=slots,
            )

    def _record(self, name: str, args: dict, result=None, error: str | None = None) -> None:
        self.calls.append(ToolCallRecord(name=name, args=args, result=result, error=error))

    # ------------------------------------------------------------------ tools

    def search_providers(
        self, service: str, area: str, date_range: dict | None = None
    ) -> list[Provider]:
        args = {"service": service, "area": area, "date_range": date_range}
        hits = [
            p
            for p in self._providers.values()
            if p.service == service and p.area == area
        ]
        if self.scenario == "recover_unavailable":
            hits = []
        # deterministic ordering, stable across runs
        hits.sort(key=lambda p: (-p.rating, p.price_paise, p.id))
        self._record("search_providers", args, hits)
        return hits

    def get_provider(self, provider_id: str) -> Provider:
        args = {"provider_id": provider_id}
        p = self._providers.get(provider_id)
        if p is None:
            self._record("get_provider", args, error="not_found")
            raise ToolError("not_found", provider_id)
        self._record("get_provider", args, p)
        return p

    def get_reviews(self, provider_id: str) -> list[Review]:
        """Not a contract tool: a projection the harness and gateway use.

        Kept separate so the untrusted text has exactly one typed home.
        """
        p = self.get_provider(provider_id)
        reviews = [
            Review("Asha R.", 5, "On time, fixed the leak, cleaned up."),
            Review("Vikram S.", 4, "Good work, slightly pricey."),
            Review("Neha K.", 3, "Had to reschedule once."),
        ]
        if p.seed_index % 3 == 0:
            reviews[1] = Review("Anonymous", 5, payloads.INJECTION)
        elif p.seed_index % 3 == 1:
            reviews[1] = Review("Anonymous", 5, payloads.INJECTION_ALT)
        return reviews

    def hold_slot(self, provider_id: str, slot: Slot, user_id: str = "u_local") -> Hold:
        args = {"provider_id": provider_id, "slot": slot, "user_id": user_id}
        p = self._providers.get(provider_id)
        if p is None:
            self._record("hold_slot", args, error="not_found")
            raise ToolError("not_found", provider_id)
        # Re-holding the same provider+slot returns the same hold and the same quote.
        # Scoped to the customer: two users can hold the same slot, and returning one
        # customer's hold to another would leak a quote and, on booking, an owner.
        for hold in self._holds.values():
            if (hold.provider_id == provider_id and hold.slot == slot
                    and hold.user_id == user_id and not self._expired(hold)):
                self._record("hold_slot", args, hold)
                return hold

        quote = p.price_paise
        if self.scenario == "price_shift":
            quote += 40000
        self._hold_seq += 1
        hold = Hold(
            hold_id=f"hold_{self._hold_seq:03d}",
            provider_id=provider_id,
            slot=slot,
            quote_paise=quote,
            expires_at=self.clock.now() + timedelta(seconds=600),
            user_id=user_id,
        )
        self._holds[hold.hold_id] = hold
        self._record("hold_slot", args, hold)
        return hold

    def _expired(self, hold: Hold) -> bool:
        return self.clock.now() > hold.expires_at

    def book(self, hold_id: str, idempotency_key: str) -> Booking:
        args = {"hold_id": hold_id, "idempotency_key": idempotency_key}
        hold = self._holds.get(hold_id)
        if hold is None:
            self._record("book", args, error="validation")
            raise ToolError("validation", f"unknown hold {hold_id}")

        if self._expired(hold):
            self._record("book", args, error="hold_expired")
            raise ToolError("hold_expired", hold_id)

        # Dedupe before the dice: a retry must return what already happened.
        if idempotency_key in self._by_key:
            existing = self._by_key[idempotency_key]
            self._record("book", args, existing)
            return existing

        forced = {
            "slot_taken": "slot_taken",
            "timeout_booked": "timeout_booked",
            "timeout_not_booked": "timeout_not_booked",
            "double_unknown": "timeout_not_booked",
        }.get(self.scenario)

        outcome = forced
        if outcome is None:
            r = self._rng.random()
            if r < 0.70:
                outcome = "success"
            elif r < 0.85:
                outcome = "slot_taken"
            else:
                # half of timeouts actually booked
                outcome = "timeout_booked" if self._rng.random() < 0.5 else "timeout_not_booked"

        if outcome == "slot_taken":
            self._record("book", args, error="slot_taken")
            raise ToolError("slot_taken", hold_id)

        if outcome == "timeout_not_booked":
            self._record("book", args, error="timeout")
            raise ToolError("timeout", hold_id)

        booking = self._make_booking(hold, idempotency_key)
        if outcome == "timeout_booked":
            # recorded, then reported as a timeout: the ambiguous case
            self._record("book", args, error="timeout")
            raise ToolError("timeout", hold_id)

        self._record("book", args, booking)
        return booking

    def _make_booking(self, hold: Hold, idempotency_key: str) -> Booking:
        self._booking_seq += 1
        p = self._providers[hold.provider_id]
        booking = Booking(
            booking_id=f"bk_{self._booking_seq:04d}",
            # The user who owns the hold, not a constant: a booking recorded under
            # somebody else's id is invisible to `list_bookings` for the person who
            # made it, which is how "move my AC servicing" could never find a
            # booking this same session had just created.
            user_id=hold.user_id,
            provider_id=hold.provider_id,
            service=p.service,
            area=p.area,
            slot=hold.slot,
            status="confirmed",
            idempotency_key=idempotency_key,
        )
        self._by_key[idempotency_key] = booking
        self._by_id[booking.booking_id] = booking
        return booking

    def get_booking_status(
        self, hold_id: str | None = None, idempotency_key: str | None = None
    ) -> dict:
        args = {"hold_id": hold_id, "idempotency_key": idempotency_key}
        if hold_id is None and idempotency_key is None:
            self._record("get_booking_status", args, error="validation")
            raise ToolError("validation", "one of hold_id or idempotency_key is required")

        if self.scenario == "double_unknown":
            out = {"status": "unknown", "booking_id": None}
            self._record("get_booking_status", args, out)
            return out

        booking = None
        if idempotency_key is not None:
            booking = self._by_key.get(idempotency_key)
        if booking is None and hold_id is not None:
            booking = next(
                (b for b in self._by_key.values() if b.idempotency_key and hold_id in b.booking_id),
                None,
            )
        out = (
            {"status": "booked", "booking_id": booking.booking_id}
            if booking
            else {"status": "not_booked", "booking_id": None}
        )
        self._record("get_booking_status", args, out)
        return out

    def cancel(self, booking_id: str, idempotency_key: str) -> Booking:
        args = {"booking_id": booking_id, "idempotency_key": idempotency_key}
        booking = self._by_id.get(booking_id)
        if booking is None:
            self._record("cancel", args, error="not_found")
            raise ToolError("not_found", booking_id)

        attempts = self._cancel_attempts.get(booking_id, 0) + 1
        self._cancel_attempts[booking_id] = attempts
        if attempts == 1 and self.scenario != "cancel_ok":
            self._record("cancel", args, error="transient")
            raise ToolError("transient", "503 Service Unavailable")

        booking.status = "cancelled"
        self._record("cancel", args, booking)
        return booking

    def reschedule(self, booking_id: str, slot: Slot, idempotency_key: str) -> Booking:
        args = {"booking_id": booking_id, "slot": slot, "idempotency_key": idempotency_key}
        booking = self._by_id.get(booking_id)
        if booking is None:
            self._record("reschedule", args, error="not_found")
            raise ToolError("not_found", booking_id)

        conflict = slot.start.hour in CONFLICT_HOURS
        if self.scenario == "reschedule_ok":
            conflict = False
        elif self.scenario == "reschedule_conflict":
            conflict = True
        if conflict:
            self._record("reschedule", args, error="slot_conflict")
            raise ToolError("slot_conflict", slot.start.isoformat())

        booking.slot = slot
        self._record("reschedule", args, booking)
        return booking

    def web_search(self, query: str) -> list:
        """Forward to the injected search backend.

        Read-only and contained: the results are `SearchHit` values whose only sink is
        the text rendered to the user. No configuration means a typed refusal, because
        a search that returns [] for "not set up" is indistinguishable from "the web
        has nothing to say" — and one of those is a lie.
        """
        args = {"query": query}
        if not str(query or "").strip():
            self._record("web_search", args, error="validation")
            raise ToolError("validation", "query is required")
        if self.web is None:
            self._record("web_search", args, error="unavailable")
            raise ToolError("unavailable", "no search backend configured")
        try:
            hits = self.web.search(query)  # type: ignore[attr-defined]
        except ToolError as exc:
            self._record("web_search", args, error=exc.error_class)
            raise
        except Exception as exc:  # the normalising boundary
            self._record("web_search", args, error="unexpected")
            raise ToolError("unexpected", f"{type(exc).__name__}: {exc}") from exc
        self._record("web_search", args, hits)
        return hits

    def list_bookings(self, user_id: str) -> list[Booking]:
        args = {"user_id": user_id}
        rows = [b for b in self._by_id.values() if b.user_id == user_id]
        rows.sort(key=lambda b: b.slot.start, reverse=True)
        self._record("list_bookings", args, rows)
        return rows

    # ------------------------------------------------------- simulated events

    def emit_provider_cancel(self, booking_id: str) -> None:
        booking = self._by_id.get(booking_id)
        if booking is None:
            raise ToolError("not_found", f"no booking {booking_id}")
        booking.status = "cancelled_by_provider"
        self.events.append({"kind": "provider_cancel", "booking_id": booking_id})

    # ------------------------------------------------------------- inspection

    def calls_named(self, name: str) -> list[ToolCallRecord]:
        return [c for c in self.calls if c.name == name]
