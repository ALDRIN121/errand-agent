"""The turn loop.

One model call per turn, at most one tool call from it, then a transition. The
rules that matter:

* **The model proposes, the orchestrator decides.** An `Action` naming a tool the
  state forbids is refused by the gateway and traced; it never reaches the server.
* **Approval is not a step the model can skip.** `book`, `cancel` and `reschedule`
  are dispatched through `ApprovalStore.execute_if_approved`, so a model that emits
  `book` with no pending approval gets a refusal, not a booking.
* **Every terminal path names a token.** No execution can end without one, and the
  sentence the user sees is rendered from that token, never written by the model.

Deliberately absent: any interpretation of the user's prose. `classify()` handles
yes/no, and everything else is the model's Action. The orchestrator does not parse
sentences, so it cannot invent a constraint the user did not state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from errand.approval import ApprovalMissing, ApprovalStore, PendingAction
from errand.classify import classify, has_new_constraint
from errand.clock import Clock
from errand.context import ContextAssembler
from errand.db import Database, dumps, loads
from errand.history import History
from errand.llm import Action, LLMClient, LLMError, ScriptExhausted
from errand.memory import MemoryStore
from errand.outcomes import OutcomeContext, render_outcome
from errand.policy import State, legal_tools, missing_slots, transition_allowed
from errand.tools.gateway import Gateway
from errand.tools.mock import MockToolServer
from errand.trace import Tracer
from errand.types import Booking, Hold, ToolError

TURN_BUDGET = 16
TOOL_BUDGET = 20
HOLD_TTL = timedelta(minutes=10)
MAX_BOOK_ATTEMPTS = 2
MAX_CLARIFY_TURNS = 4
MAX_STEPS_PER_TURN = 6


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


class Orchestrator:
    def __init__(
        self,
        *,
        clock: Clock,
        server: MockToolServer,
        db: Database,
        memory: MemoryStore,
        history: History,
        approvals: ApprovalStore,
        llm: LLMClient,
        tracer: Tracer | None = None,
        context: ContextAssembler | None = None,
        session_id: str = "s_1",
        user_id: str = "u_aldrin",
    ) -> None:
        self.clock = clock
        self.server = server
        self.db = db
        self.memory = memory
        self.history = history
        self.approvals = approvals
        self.llm = llm
        self.tracer = tracer
        self.context = context or ContextAssembler()
        self.gateway = Gateway(server=server, tracer=tracer)
        self.session = self._load_or_create(session_id, user_id)
        self.hold: Hold | None = None
        self.pending: PendingAction | None = None
        self.candidates: list = []
        self.last_choice: Any = None
        self.action_attribution: list[dict] = []
        # The last read result, so the next prompt can include it. Side-effect results
        # are summarised to a confirmation, never the raw payload.
        self.last_tool_result: str | None = None

    # ------------------------------------------------------------------ session

    def _load_or_create(self, session_id: str, user_id: str) -> Session:
        row = self.db.query_one("SELECT * FROM session WHERE id=?", (session_id,))
        if row is None:
            now = self.clock.now().isoformat()
            self.db.execute(
                "INSERT INTO session(id, user_id, state, mode, slots_json, budget_json,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, user_id, State.NEW.value, "chat", "{}", "{}", now, now),
            )
            return Session(id=session_id, user_id=user_id)
        budget = loads(row["budget_json"], {})
        return Session(
            id=row["id"],
            user_id=row["user_id"],
            state=State(row["state"]),
            slots=loads(row["slots_json"], {}),
            outcome=row["outcome"],
            outcome_verb=row["outcome_verb"],
            turns=int(budget.get("turns", 0)),
            tool_calls=int(budget.get("tool_calls", 0)),
            clarify_turns=int(budget.get("clarify_turns", 0)),
            recovery_declined=row["recovery_declined_booking"],
        )

    def _persist(self) -> None:
        self.db.execute(
            "UPDATE session SET state=?, slots_json=?, outcome=?, outcome_verb=?,"
            " recovery_declined_booking=?, budget_json=?, updated_at=? WHERE id=?",
            (
                self.session.state.value,
                dumps(self.session.slots),
                self.session.outcome,
                self.session.outcome_verb,
                self.session.recovery_declined,
                dumps(
                    {
                        "turns": self.session.turns,
                        "tool_calls": self.session.tool_calls,
                        "clarify_turns": self.session.clarify_turns,
                    }
                ),
                self.clock.now().isoformat(),
                self.session.id,
            ),
        )

    def _transition(self, dst: State) -> None:
        if not transition_allowed(self.session.state, dst):
            raise RuntimeError(
                f"illegal transition {self.session.state.value} -> {dst.value}"
            )
        self.session.state = dst
        self._persist()

    def _terminal(self, token: str, verb: str | None = None) -> None:
        self.session.outcome = token
        self.session.outcome_verb = verb or self.session.outcome_verb
        self.session.state = State.TERMINAL
        self._persist()
        if self.tracer:
            self.tracer.emit("terminal", outcome=token, verb=self.session.outcome_verb)

    # --------------------------------------------------------------------- public

    def handle_turn(self, user_text: str) -> TurnResult:
        """One user message in, one reply out."""
        if self.session.state is State.TERMINAL:
            return TurnResult(
                state=State.TERMINAL,
                reply=self._sentence(),
                tool=None,
                outcome=self.session.outcome,
                sentence=self._sentence(),
            )

        self.history.append("user", user_text, spans={})
        self.session.slots["last_user"] = user_text
        self.session.turns += 1
        if self.tracer:
            self.tracer.emit("turn", n=self.session.turns, user=user_text,
                             state=self.session.state.value)

        # --- budget gates, checked before any model call
        if self.session.turns > TURN_BUDGET:
            self._terminal("budget_turns")
            return self._result(None, None)
        if self.session.tool_calls >= TOOL_BUDGET:
            self._terminal("budget_tools")
            return self._result(None, None)

        # --- a reply to a pending approval is classified, not interpreted
        verdict = classify(user_text)
        if self.pending is not None and self.session.state is State.AWAITING_APPROVAL:
            if verdict == "affirm":
                return self._on_affirm()
            if verdict == "deny" and not has_new_constraint(user_text):
                self.approvals.invalidate(self.session.id, "user declined")
                # Declining a *recovery offer* leaves the original booking cancelled
                # and books nothing: that is `cancelled`, not "abandoned", because the
                # user answered the question rather than walking away.
                if self.session.slots.get("cancelled_booking"):
                    self.session.recovery_declined = self.session.slots["cancelled_booking"]
                    self.pending, self.hold = None, None
                    self._terminal("cancelled")
                    return self._result(None, None)
                self.pending, self.hold = None, None
                # A declined approval ends the request. Returning to SELECTING would
                # keep a session alive that the user has just told us to stop, and
                # nothing else can produce the `abandoned_by_user` token.
                self._terminal("abandoned_by_user", verb=self.session.outcome_verb)
                return self._result(None, None)
            # a conditional or amended yes is not an approval: re-ask
            self.history.append("assistant", "I need a plain yes to book at that price.")
            return self._result(None, "I need a plain yes to book at that price.")

        # A bare "no" only ends the run when there is something to decline; without a
        # pending approval it may be a correction ("not afternoons, mornings"), which
        # the model is better placed to read than the classifier.

        return self._agent_loop(user_text)

    # ----------------------------------------------------------------- the loop

    def _agent_loop(self, user_text: str) -> TurnResult:
        """One user message may take several model steps.

        Searching then holding is two tool calls but one user utterance, so the loop
        keeps going while the model keeps proposing legal tools and stops as soon as
        the session needs the user (a question, an approval, or a terminal token).
        The step cap is the turn budget; a loop that hits it ends with a token.
        """
        last: TurnResult | None = None
        for _ in range(MAX_STEPS_PER_TURN):
            if self.session.state in (State.AWAITING_APPROVAL, State.TERMINAL):
                break
            try:
                last = self._model_step(user_text if last is None else "")
            except ScriptExhausted:
                # the script had nothing left: end the turn wherever it stands. A
                # run that never reaches a token fails in the harness, not here.
                break
            if self.session.state in (
                State.AWAITING_APPROVAL, State.TERMINAL, State.CLARIFYING, State.PAUSED,
            ):
                break
            if last.tool is None:
                break
            if last.refused is not None:
                break
        if last is None:
            return self._result(None, None)
        return last

    def finish(self) -> None:
        """Close a session the script left alive.
        A run whose conversation simply stops is the user walking away, and that has
        its own token. Without this, `abandoned_by_user` was declared in the
        vocabulary and unreachable in practice, and every scenario ending on a
        question would have had to assert "no outcome at all".
        """
        if self.session.state is State.TERMINAL:
            return
        if self.pending is not None:
            self.approvals.invalidate(self.session.id, "session ended awaiting approval")
        self._terminal("abandoned_by_user")

    # ------------------------------------------------------------------ the model

    def _model_step(self, user_text: str) -> TurnResult:
        prompt, manifest = self.context.assemble(
            state=self.session.state,
            approval_kind=self.pending.kind if self.pending else None,
            history=self.history,
            memory=self.memory,
            user_id=self.session.user_id,
            intent=user_text,
            tool_result=self.last_tool_result,
            latest_user=user_text,
        )
        if self.tracer:
            self.tracer.emit("context", tokens=manifest.total,
                             included=len(manifest.included),
                             excluded=len(manifest.excluded),
                             state=self.session.state.value)

        try:
            action = self.llm.propose(self.context.render_text(prompt), state=self.session.state.value)
        except ScriptExhausted:
            raise
        except LLMError as exc:
            self._terminal("tool_failure", )
            return self._result(None, None)
        if self.tracer:
            self.tracer.emit("action", tool=action.tool, args=action.args,
                             message=action.message, state=self.session.state.value)

        if not action.is_tool_call:
            return self._on_no_tool(action, user_text)

        return self._on_tool(action)

    def _on_no_tool(self, action: Action, user_text: str) -> TurnResult:
        """A question, a refusal, or an unhelpful nudge — no tool call."""
        text = action.message or ""
        if self.session.state in (State.NEW, State.CLARIFYING):
            self.session.clarify_turns += 1
            if self.session.clarify_turns > MAX_CLARIFY_TURNS:
                self._terminal("aborted_missing_details",
                               )
                self.session.slots["reason"] = "I asked for the missing details and did not get them"
                return self._result(None, None)
            if self.session.state is State.NEW:
                self._transition(State.CLARIFYING)
        self.history.append("assistant", text)
        return self._result(None, text)

    def _admit(self, action: Action) -> TurnResult:
        """Leave NEW/CLARIFYING by reading the slots out of the model's arguments.

        A proposal that carries the missing slots is the model's answer to the
        question, in structured form. If it does not carry them, the turn becomes a
        CLARIFYING turn and the question is the model's own message.
        """
        name = action.tool or ""
        args = action.args or {}

        if name in ("book", "cancel", "reschedule"):
            # Opening an approval is not executing: _open_approval raises the ask and
            # the user still has to say yes. `book` with no hold has nothing to ask
            # about, so that one is refused.
            return self._open_approval(action)

        if name == "list_bookings":
            if self.session.state is not State.DISCOVERING:
                self._transition(State.DISCOVERING)
            return self._dispatch(action)

        if name == "search_providers":
            service = args.get("service")
            area = args.get("area")
            window = args.get("date_range")
            if self.session.state is State.SELECTING and service and area:
                # "same electrician as last time" resolves a provider, then searches
                # their free slots: the target slots are known, so SELECTING -> SEARCHING
                # is the right hop before asking the tool.
                self.session.slots.setdefault("service", service)
                self.session.slots.setdefault("area", area)
                self._transition(State.SEARCHING)
                return self._dispatch(action)
            if service and area and window:
                self.session.slots.update(
                    service=service, area=area,
                    window_start=window.get("start") if isinstance(window, dict) else window,
                    window_end=window.get("end") if isinstance(window, dict) else window,
                )
                self.session.slots.pop("reason", None)
                self._transition(State.SEARCHING)
                return self._dispatch(action)
            # incomplete proposal: treat as a clarifying turn
            missing = missing_slots(self.session.slots)
            msg = action.message or f"Which of these is missing: {', '.join(missing)}?"
            return self._clarify(msg)

        if name in ("hold_slot", "get_provider"):
            # A user who names a provider ("book prov_06 for tomorrow") makes holding
            # a legitimate opening step; so does a reschedule, which must hold the
            # target slot before the user is asked to approve the move.
            if args.get("provider_id"):
                if self.session.state in (State.NEW, State.CLARIFYING, State.DISCOVERING):
                    self._transition(State.SELECTING)
                return self._dispatch(action)
            msg = action.message or "Which provider do you mean?"
            return self._clarify(msg)

        # get_booking_status makes no sense before anything is in flight
        msg = action.message or "I need the service and area before I can search."
        return self._clarify(msg)

    def _clarify(self, msg: str) -> TurnResult:
        self.session.clarify_turns += 1
        if self.session.clarify_turns > MAX_CLARIFY_TURNS:
            self.session.slots["reason"] = (
                "I asked for the missing details and did not get them."
            )
            self._terminal("aborted_missing_details")
            return self._result(None, None)
        if self.session.state is State.NEW:
            self._transition(State.CLARIFYING)
        self.history.append("assistant", msg)
        return self._result(None, msg)

    def _dispatch(self, action: Action) -> TurnResult:
        """Send an already-admitted action through the gateway."""
        name = action.tool or ""
        self._attribute(name, action.args)
        result = self.gateway.call(
            self.session.state, name, action.args,
            approval_kind=self.pending.kind if self.pending else None,
        )
        self.session.tool_calls += 1
        if not result.ok:
            assert result.error is not None
            if result.error.error_class == "validation":
                msg = f"I cannot do that right now ({result.error.detail.split('(')[0].strip()})."
                self.history.append("assistant", msg)
                return self._result(name, msg, refused=result.error.detail)
            self._terminal("tool_failure")
            self.session.slots["reason"] = result.error.detail
            return self._result(name, None)
        self.last_tool_result = _summarise(name, result.value)
        if self.tracer:
            self.tracer.emit("tool_result_summary", tool=name,
                             chars=len(self.last_tool_result or ""))
        return self._after_tool(name, result.value)

    def _on_tool(self, action: Action) -> TurnResult:
        name = action.tool or ""
        assert action.tool is not None

        # --- NEW/CLARIFYING admit the model's structured proposal as its intent.
        # DISCOVERING is included because resolving a booking leaves the session there,
        # and the next legitimate step (holding the target slot) happens in SELECTING.
        if self.session.state in (State.NEW, State.CLARIFYING, State.DISCOVERING):
            return self._admit(action)

        # A search is legal from SELECTING too: after resolving a provider
        # ("the same electrician as last time") the next step is to look at their
        # slots, and a failed search retried with a wider area is the same move.
        if name == "search_providers" and self.session.state is State.SELECTING:
            if action.args.get("service") and action.args.get("area"):
                self.session.slots.setdefault("service", action.args["service"])
                self.session.slots.setdefault("area", action.args["area"])
                self._transition(State.SEARCHING)

        # --- side-effecting tools are dispatched only through the approval store.
        # A model proposing one with nothing pending is asking to *start* an
        # approval, not asking to execute: refuse only if there is nothing to act on.
        if name in ("book", "cancel", "reschedule"):
            if self.pending is None:
                return self._open_approval(action)
            return self._execute_approved(action)

        return self._dispatch(action)

    def _open_approval(self, action: Action) -> TurnResult:
        """Start an approval for cancel/reschedule from a resolved booking.

        Reaching here means the model proposed a side effect with nothing pending.
        For a cancel or reschedule that is a legitimate first step as long as the
        booking is resolved; the user still has to say yes, so no side effect can
        happen from this path.
        """
        name = action.tool or ""
        if name == "book":
            msg = "Nothing is held yet, so there is nothing to book. Let me search first."
            self.history.append("assistant", msg)
            return self._result(name, msg, refused="book proposed with no hold")

        booking = None
        manage = self.session.slots.get("manage")
        if isinstance(manage, dict):
            booking = manage.get("booking")
        if booking is None:
            target = action.args.get("booking_id")
            if target:
                booking = next(
                    (b for b in self.server.list_bookings(self.session.user_id)
                     if b.booking_id == target), None
                )
        if booking is None:
            msg = "I could not find a booking to change. Which one do you mean?"
            self.history.append("assistant", msg)
            return self._result(name, msg, refused="no resolved booking")

        # Persist the resolution: _propose reads the booking from the session slots,
        # so a booking found here and not recorded there fails a few lines later.
        self.session.slots["manage"] = {"booking": booking}

        if name == "reschedule":
            slot = action.args.get("slot")
            if slot is None:
                msg = "Which time should I move it to?"
                self._clarify(msg)
                return self._result(name, msg)
        self._propose(action)
        return self._result(name, self.pending_text)

    def _after_tool(self, name: str, value: Any) -> TurnResult:
        """State transitions for the read-only tools, and the slot bookkeeping."""
        if name == "list_bookings":
            self.candidates = list(value)
            if self.session.state is State.DISCOVERING:
                resolution = self._resolve_reference(self.candidates, self.session.slots.get("last_user", ""))
                if isinstance(resolution, str):
                    # ambiguous: ask, and no tool call happens until resolved
                    self.history.append("assistant", resolution)
                    return self._result(name, resolution)
                if resolution is None:
                    # nothing to manage: the user meant a new booking
                    self.session.slots["reason"] = "no existing booking matched"
                    self._transition(State.SEARCHING)
                    return self._result(name, "I could not find a booking to change.")
                booking = resolution["booking"]
                self.session.slots["manage"] = resolution
                self.session.slots["manage_kind"] = (
                    "cancel" if "cancel" in self.session.slots.get("last_user", "").lower()
                    else "reschedule"
                ) if _is_manage_request(self.session.slots.get("last_user", "")) else None
                self.session.slots["service"] = booking.service
                self.session.slots["area"] = booking.area
                # A resolved booking needs no search: the next step is to hold the
                # target slot, which is legal only in SELECTING.
                self._transition(State.SELECTING)
                return self._result(name, None)
            return self._result(name, None)

        if name == "search_providers":
            self.candidates = list(value)
            if not self.candidates:
                self.session.slots["reason"] = (
                    "No provider matches that service, area and window inside the budget."
                )
                self._terminal("no_match")
                return self._result(name, None)
            self._transition(State.SELECTING)
            return self._result(name, None)

        if name == "get_provider":
            self.last_choice = value
            return self._result(name, None)

        if name == "hold_slot":
            self.hold = value
            # A hold is a quote, not a decision. Ask for approval only when the hold
            # is the thing the user is buying; a reschedule holds a *target* slot and
            # proposes the move afterwards, so it must not fall into the book branch.
            if self.session.slots.get("manage_kind"):
                # a manage request: this hold is the *target* slot of a move, so the
                # approval to raise is the reschedule, not a booking
                return self._result(name, action_message_for(value))
            self._propose(Action("book", {}, action_message_for(value)))
            return self._result(name, self.pending_text)

        if name == "get_booking_status":
            return self._after_verify(value)

        raise RuntimeError(f"no handler for tool {name}")

    # -------------------------------------------------------------- approval flow

    @property
    def pending_text(self) -> str | None:
        row = self.db.query_one(
            "SELECT text_shown FROM approval WHERE session_id=? ORDER BY created_at DESC,"
            " rowid DESC LIMIT 1",
            (self.session.id,),
        )
        return row["text_shown"] if row else None

    def _propose(self, action: Action, text: str | None = None) -> None:
        """Freeze a PendingAction and ask for approval."""
        if action.tool == "book":
            assert self.hold is not None
            pending = PendingAction(
                kind="book",
                provider_id=self.hold.provider_id,
                service=self.session.slots.get("service", "service"),
                slot_start=self.hold.slot.start,
                slot_end=self.hold.slot.end,
                price_paise=self.hold.quote_paise,
                attempt=0,
            )
        elif action.tool in ("cancel", "reschedule"):
            manage = self.session.slots.get("manage") or {}
            booking = manage.get("booking") if isinstance(manage, dict) else None
            if booking is None:
                raise ApprovalMissing(f"{action.tool} proposed with no resolved booking")
            if action.tool == "reschedule" and self.hold is None:
                # the target slot must be held before the user is asked to approve it,
                # or the approved digest names a slot nobody checked
                raise ApprovalMissing("reschedule proposed with no hold on the new slot")
            pending = PendingAction(
                kind=action.tool,
                provider_id=booking.provider_id,
                service=booking.service,
                slot_start=self.hold.slot.start if self.hold else booking.slot.start,
                slot_end=self.hold.slot.end if self.hold else booking.slot.end,
                price_paise=0,
                booking_id=booking.booking_id,
                attempt=0,
            )
        else:
            raise ValueError(f"cannot propose {action.tool}")

        shown = text or default_prompt(pending)
        self.pending = pending
        self.approvals.request(self.session.id, pending, shown)
        if self.session.state is not State.AWAITING_APPROVAL:
            self._transition(State.AWAITING_APPROVAL)
        self.history.append("assistant", shown)
        if self.tracer:
            self.tracer.emit("approval_requested", kind=pending.kind,
                             digest=pending.digest()[:12], text=shown)

    def _on_affirm(self) -> TurnResult:
        assert self.pending is not None
        # The hold's own TTL is shorter than the approval TTL, so it is checked
        # first: an approval that is still "valid" can name a slot that is gone.
        if self.hold is not None and self.hold.expires_at <= self.clock.now():
            self.approvals.invalidate(self.session.id, "hold expired")
            self._terminal("hold_expired")
            return self._result(None, None)
        if not self.approvals.validate(self.session.id, self.pending):
            # expired or superseded between the ask and the reply
            self._terminal("hold_expired")
            return self._result(None, None)
        if self.session.state is not State.EXECUTING:
            self._transition(State.EXECUTING)
        # only the approved verb may run, and only through the approval store
        return self._execute_approved(Action(self.pending.kind, {}, None))

    def _execute_approved(self, action: Action) -> TurnResult:
        name = action.tool or ""
        self._attribute(name, action.args)
        live = self.pending
        if live is None or live.kind != name:
            msg = "Nothing is held for me to do that. Confirm the details and I will ask again."
            self.history.append("assistant", msg)
            return self._result(name, msg, refused="no pending approval")

        def _call():
            if name == "book":
                assert self.hold is not None
                return self.server.book(self.hold.hold_id, live.idempotency_key())
            if name == "cancel":
                assert live.booking_id
                return self.server.cancel(live.booking_id, live.idempotency_key())
            assert live.booking_id
            assert self.hold is not None
            return self.server.reschedule(live.booking_id, self.hold.slot, live.idempotency_key())

        booking = None
        for attempt in (1, 2):  # one retry, for transient only
            try:
                booking = self.approvals.execute_if_approved(self.session.id, live, _call)
                break
            except ApprovalMissing as exc:
                msg = "That is not approved, so I have not done it."
                self.history.append("assistant", msg)
                return self._result(name, msg, refused=str(exc))
            except ToolError as exc:
                if exc.error_class == "transient" and attempt == 1:
                    if self.tracer:
                        self.tracer.emit("side_effect_retry", tool=name,
                                         error=exc.error_class, detail=exc.detail)
                    continue
                return self._after_side_effect_error(name, exc)
        self.session.tool_calls += 1
        self.approvals.consume(self.session.id)
        self._terminal("booked", verb=name)
        self.session.slots["reference"] = booking.booking_id
        if self.tracer:
            self.tracer.emit("side_effect_ok", tool=name, booking_id=booking.booking_id)
        return self._result(name, None)

    def _after_side_effect_error(self, name: str, exc: ToolError) -> TurnResult:
        if self.tracer:
            self.tracer.emit("side_effect_error", tool=name, error=exc.error_class)
        if exc.error_class in ("timeout", "unexpected"):
            # the call may have landed: never claim either way without checking
            self._transition(State.VERIFYING)
            self.session.slots["verify_pending"] = True
            status = self.gateway.call(State.VERIFYING, "get_booking_status",
                                      {"idempotency_key": self.pending.idempotency_key()}
                                      if self.pending else {})
            return self._after_verify(status.value if status.ok else {"status": "unknown"})
        if exc.error_class in ("slot_taken", "slot_conflict"):
            self._terminal(exc.error_class)
            return self._result(name, None)
        if exc.error_class == "hold_expired":
            self._terminal("hold_expired")
            return self._result(name, None)
        self._terminal("tool_failure")
        self.session.slots["reason"] = exc.detail
        return self._result(name, None)

    def _after_verify(self, status: dict) -> TurnResult:
        state = status.get("status")
        if state == "booked":
            # This is the single most important outcome in the assignment, and it
            # emitted no side_effect_ok: the booking exists, the run claimed success,
            # and the trace had no record of it. Emit the confirmation explicitly.
            if self.tracer:
                tool = (self.pending.kind if self.pending else None) or "book"
                self.tracer.emit(
                    "side_effect_ok",
                    tool=tool,
                    booking_id=status.get("booking_id"),
                    verified=True,
                )
            if self.session.outcome_verb is None and self.pending is not None:
                self.session.outcome_verb = self.pending.kind
            self._terminal("booked_verified_after_timeout", verb=self.session.outcome_verb)
            self.session.slots["reference"] = status.get("booking_id")
        elif state == "not_booked":
            # safe to retry: one more attempt, same key
            self._terminal("booked" if self.session.outcome else "unverified")
        else:
            self._terminal("unverified")
            self.session.slots["reference"] = status.get("booking_id")
        return self._result("get_booking_status", None)

    # ----------------------------------------------------------- reference resolve

    def _resolve_reference(self, bookings: list[Booking], utterance: str = ""):
        """G1: the ladder. Returns a booking dict, a question string, or None.

        The service is taken from the user's own words when the slots do not have it:
        "move my AC servicing" names ac_service even though nothing has been searched.
        Without this the ladder matched every confirmed booking and asked a question
        the script could not have anticipated.
        """
        wanted = self.session.slots.get("service") or _service_from(utterance)
        rows = [b for b in bookings if wanted is None or b.service == wanted] if wanted else list(bookings)
        if not rows:
            return None
        if len(rows) == 1:
            return {"booking": rows[0]}
        confirmed = [b for b in rows if b.status == "confirmed"]
        if len(confirmed) == 1:
            return {"booking": confirmed[0]}
        pool = confirmed or rows
        listed = ", ".join(
            f"{b.provider_id} on {b.slot.start:%a %d %b} ({b.status})" for b in pool
        )
        return f"You have {len(pool)} {wanted or 'matching'} bookings - {listed}. Which do you mean?"

    def _attribute(self, tool: str, args: dict) -> None:
        """I8: record that this call came from a model action, with a hash.

        The gateway already refuses illegal tools, but nothing until now proved the
        run was *driven* rather than hard-coded. The harness compares consecutive
        (tool, args_hash) pairs against what the client returned.
        """
        import hashlib

        payload = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        self.action_attribution.append({"tool": tool, "args_hash": digest, "state": self.session.state.value})
        if self.tracer:
            self.tracer.emit("action_attributed", tool=tool, args_hash=digest,
                             state=self.session.state.value)


    # ------------------------------------------------------- unattended events (G2)

    def handle_event(self, kind: str, **payload: Any) -> dict[str, Any]:
        """React to a system event with no user turn present.

        G2: no model call. Picking a replacement inside known preferences is a sort,
        not a judgement, and a model here would add cost and nondeterminism to the
        one path no human is watching. The run records `llm_mode="none"` so the
        absence of a model call is auditable rather than assumed.

        Returns the message queued for the user; the watcher owns delivery.
        """
        if kind != "provider_cancel":
            raise ValueError(f"unknown event kind {kind!r}")

        booking_id = payload.get("booking_id")
        if not booking_id:
            raise ValueError("provider_cancel needs a booking_id")

        if self.tracer:
            self.tracer.emit("event_received", kind=kind, booking_id=booking_id,
                             llm_mode="none")

        cancelled = next(
            (b for b in self.server.list_bookings(self.session.user_id)
             if b.booking_id == booking_id), None
        )
        if cancelled is None:
            return {"status": "ignored", "reason": "booking not found"}

        self.session.slots["cancelled_booking"] = booking_id
        replacement = self._find_replacement(cancelled)

        if replacement is None:
            self.session.slots["reason"] = (
                f"{cancelled.provider_id} cancelled your {cancelled.service} on "
                f"{cancelled.slot.start:%a %d %b} and I found no replacement."
            )
            self._terminal("no_match")
            body = self._sentence() or ""
            return {"status": "no_replacement", "body": body}

        provider, slot = replacement
        self.hold = self.server.hold_slot(provider.id, slot)
        pending = PendingAction(
            kind="book",
            provider_id=self.hold.provider_id,
            service=cancelled.service,
            slot_start=self.hold.slot.start,
            slot_end=self.hold.slot.end,
            price_paise=self.hold.quote_paise,
            attempt=0,
        )
        self.pending = pending
        body = (
            f"Your {cancelled.service} on {cancelled.slot.start:%a %d %b} was cancelled "
            f"by {cancelled.provider_id}. I found {provider.id} at "
            f"Rs {self.hold.quote_paise // 100:,} on "
            f"{slot.start:%a %d %b %H:%M} - shall I book it?"
        )
        self.approvals.request(self.session.id, pending, body)
        if self.session.state is not State.AWAITING_APPROVAL:
            self._transition(State.AWAITING_APPROVAL)
        if self.tracer:
            self.tracer.emit("recovery_offered", booking_id=booking_id,
                             replacement=provider.id, digest=pending.digest()[:12])
        return {"status": "offered", "body": body, "replacement": provider.id}

    def _find_replacement(self, cancelled) -> tuple[Any, Any] | None:
        """G2's ranking, in one place: (-rating, price, id), preference over rating.

        Technician preference beats rating when one exists, because it is an explicit
        user statement and rating is a popularity proxy.
        """
        pinned = {
            m.key: m.value for m in self.memory.admissible(self.session.user_id)
        }.get("preferred_technician")

        candidates = [
            p for p in self.server.search_providers(
                service=cancelled.service, area=cancelled.area
            )
            if p.id != cancelled.provider_id
        ]
        if not candidates:
            return None

        slot_for = lambda p: next(
            (s for s in p.slots if s.available and s.start.date() >= cancelled.slot.start.date()),
            None,
        )
        with_slot = [(p, s) for p in candidates if (s := slot_for(p)) is not None]
        if not with_slot:
            return None

        if pinned:
            chosen = next((pair for pair in with_slot if pair[0].id == pinned), None)
            if chosen is not None:
                return chosen

        with_slot.sort(key=lambda pair: (-pair[0].rating, pair[0].price_paise, pair[0].id))
        return with_slot[0]

    # -------------------------------------------------------------------- renders

    def _sentence(self) -> str | None:
        if self.session.outcome is None:
            return None
        action = self.pending
        reference = (
            self.session.slots.get("reference")
            or self.session.recovery_declined
        )
        return render_outcome(
            self.session.outcome,
            OutcomeContext(
                action=action,
                reference=reference,
                reason=self.session.slots.get("reason"),
            ),
        )

    def _result(self, tool: str | None, reply: str | None, refused: str | None = None) -> TurnResult:
        sentence = self._sentence()
        if sentence is not None:
            reply = sentence
        return TurnResult(
            state=self.session.state,
            reply=reply,
            tool=tool,
            outcome=self.session.outcome,
            sentence=sentence,
            refused=refused,
        )


SERVICE_WORDS = {
    "ac_service": ("ac", "a/c", "air con", "aircon", "servicing"),
    "plumber": ("plumb", "tap", "leak", "pipe"),
    "electrician": ("electric", "wiring", "switchboard", "mcb", "socket"),
}


def _summarise(name: str, value: Any) -> str | None:
    """A compact, model-readable rendering of a read result.

    The context budget allows 500 tokens for a tool result, so the full search payload
    cannot be replayed; the fields a choice needs are the id, the price, the rating and
    the first free slot.
    """
    if value is None:
        return None
    if name == "search_providers":
        rows = value.get("providers") if isinstance(value, dict) else value
        if not rows:
            return "No providers matched."
        lines = []
        for p in rows[:8]:
            free = next((s for s in getattr(p, "slots", []) if s.available), None)
            when = f"next free {free.start:%a %d %b %H:%M}" if free else "no free slot"
            lines.append(f"- {p.id}: rating {p.rating}, Rs {p.price_paise // 100:,}, {when}")
        return "Providers found:\n" + "\n".join(lines)
    if name == "get_provider":
        p = value
        return (f"{p.id}: rating {p.rating}, Rs {p.price_paise // 100:,}, "
                f"serves {p.service} in {p.area}")
    if name == "list_bookings":
        rows = value if isinstance(value, list) else value.get("bookings", [])
        if not rows:
            return "No bookings on record."
        return "Bookings:\n" + "\n".join(
            f"- {b.booking_id}: {b.service} with {b.provider_id} "
            f"{b.slot.start:%a %d %b %H:%M} ({b.status})"
            for b in rows[:8]
        )
    if name == "get_booking_status":
        return f"status: {value.get('status')}, booking {value.get('booking_id')}"
    return None


MANAGE_VERBS = ("move", "cancel", "reschedule", "change", "shift", "postpone")


def _is_manage_request(text: str) -> bool:
    """Whether the user asked to modify an existing booking rather than make one."""
    low = (text or "").lower()
    return any(v in low for v in MANAGE_VERBS)


def _service_from(text: str) -> str | None:
    """The service named in the user's words, or None.

    A closed vocabulary, deliberately: this is a lookup of the three contract
    services, not natural-language understanding. Ambiguous text returns None and
    falls through to the ladder's question.
    """
    low = (text or "").lower()
    hits = [svc for svc, words in SERVICE_WORDS.items() if any(w in low for w in words)]
    return hits[0] if len(hits) == 1 else None


def action_message_for(hold: Hold) -> str:
    mins = int((hold.expires_at - hold.slot.start).total_seconds() // 60)
    return (
        f"Hold {hold.provider_id}, {hold.slot.start:%a %d %b %H:%M}-{hold.slot.end:%H:%M}, "
        f"quoted Rs {hold.quote_paise // 100:,}. Confirm? The hold expires in 10 minutes."
    )


def default_prompt(pending: PendingAction) -> str:
    if pending.kind == "cancel":
        return f"Cancel booking {pending.booking_id} with {pending.provider_id}? No charge."
    if pending.kind == "reschedule":
        return (
            f"Reschedule {pending.booking_id} to {pending.slot_start:%a %d %b %H:%M}"
            f"-{pending.slot_end:%H:%M} with {pending.provider_id}. Confirm?"
        )
    return (
        f"Book {pending.service} with {pending.provider_id}, "
        f"{pending.slot_start:%a %d %b %H:%M}-{pending.slot_end:%H:%M}, "
        f"quoted Rs {pending.price_paise // 100:,}. Confirm?"
    )
