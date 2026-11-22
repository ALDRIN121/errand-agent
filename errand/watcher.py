"""The unattended path: drain server events, queue one message each.

No threads, no sleeping, no timer. `tick()` is called at start-up (so an event
raised while the process was down still fires) and after every CLI turn. That makes
the proactive path testable by calling `tick()` directly, with no wall-clock
dependence and no second agent.

The watcher owns delivery; the orchestrator owns the decision. Keeping them apart is
what lets `handle_event` stay a pure rules path (G2) while delivery stays swappable.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from errand.clock import Clock
from errand.db import Database
from errand.orchestrator import Orchestrator
from errand.tools.mock import MockToolServer
from errand.trace import Tracer


@dataclass
class Watcher:
    server: MockToolServer
    orchestrator: Orchestrator
    db: Database
    clock: Clock
    tracer: Tracer | None = None
    handled: set[int] = field(default_factory=set)

    def tick(self) -> list[str]:
        """Drain every unprocessed event. Returns the bodies queued this tick.

        Idempotent: an event is keyed by its index in `server.events`, so a second
        `tick()` with no new events queues nothing. That property is what
        `test_watcher` asserts, because a proactive path that re-fires is a path that
        spams the user.
        """
        queued: list[str] = []

        for index, event in enumerate(list(self.server.events)):
            if index in self.handled:
                continue
            self.handled.add(index)

            payload = dict(event)
            kind = payload.pop("kind")
            try:
                result = self.orchestrator.handle_event(kind, **payload)
            except Exception as exc:  # an event must never kill the loop
                if self.tracer:
                    self.tracer.emit("event_failed", kind=kind, error=str(exc))
                continue

            body = result.get("body") or ""
            if not body:
                continue
            self._enqueue(body)
            queued.append(body)

        return queued

    def _enqueue(self, body: str) -> None:
        """One outbox row per event. `delivered_at` is set when the CLI renders it."""
        self.db.execute(
            "INSERT INTO outbox(session_id, body, created_at) VALUES (?,?,?)",
            (self.orchestrator.session.id, body, self.clock.now().isoformat()),
        )
        if self.tracer:
            self.tracer.emit("outbox_queued", session=self.orchestrator.session.id,
                             body_chars=len(body))

    # ------------------------------------------------------------------ delivery

    def pending(self) -> list[dict]:
        rows = self.db.query(
            "SELECT id, body, created_at FROM outbox WHERE delivered_at IS NULL"
            " ORDER BY id ASC"
        )
        return [dict(r) for r in rows]

    def deliver(self, row_id: int) -> None:
        self.db.execute(
            "UPDATE outbox SET delivered_at=? WHERE id=?",
            (self.clock.now().isoformat(), row_id),
        )

    def drain_for_display(self) -> list[str]:
        """Mark everything pending delivered and return the bodies, in order."""
        pending = self.pending()
        for row in pending:
            self.deliver(row["id"])
        return [row["body"] for row in pending]
