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
from datetime import timedelta
from typing import Any

from errand.admission import admit
from errand.approval import ApprovalMissing, ApprovalStore, PendingAction
from errand.areas import known_areas, resolve_area
from errand.classify import classify, has_new_constraint
from errand.clock import Clock
from errand.context import ContextAssembler
from errand.db import Database, dumps, loads
from errand.dialogue import ask_which, elsewhere
from errand.execution import after_verify, execute_approved
from errand.history import History
from errand.llm import Action, LLMClient, LLMError, ScriptExhausted
from errand.memory import MemoryStore
from errand.outcomes import OutcomeContext, render_outcome
from errand.policy import State, missing_slots, transition_allowed
from errand.recovery import find_replacement
from errand.session import Session, TurnResult
from errand.tools.gateway import Gateway
from errand.tools.mock import MockToolServer
from errand.trace import Tracer
from errand.types import Booking, Hold
from errand.wording import (
    action_message_for,
    default_prompt,
    is_manage_request,
    plain_slots,
    service_from,
    summarise,
)

TURN_BUDGET = 16
TOOL_BUDGET = 20
HOLD_TTL = timedelta(minutes=10)
MAX_BOOK_ATTEMPTS = 2
# Ask once, then stop. A second question with no answer behind it is the agent
# interrogating a user who has already declined to elaborate; the honest move is to
# leave with a sentence saying what is missing, not to keep asking.
MAX_CLARIFY_TURNS = 1
MAX_STEPS_PER_TURN = 6

# Human-readable progress. Internal event names (`tool_refused`, `_dispatch`) are
# useless to a person watching the screen, and a UI that invents its own wording
# drifts from the trace. One table, one place.
PROGRESS: dict[str, str] = {
    "thinking": "Thinking about what to do next",
    "searching": "Searching our provider list",
    "provider": "Looking at one provider in detail",
    "holding": "Holding the slot",
    "research": "Searching the web",
    "research_done": "Read the results",
    "approval": "Waiting for your approval",
    "executing": "Carrying out the approved action",
    "verifying": "Confirming with the provider",
    "refused": "That action was not allowed here, so nothing happened",
    "retry": "A transient error; retrying once",
    "done": "Done",
}


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
        self._last_question: str = ""     # the previous question, to spot a repeat
        self.last_choice: Any = None
        self.action_attribution: list[dict] = []
        # The last read result, so the next prompt can include it. Side-effect results
        # are summarised to a confirmation, never the raw payload.
        self.last_tool_result: str | None = None
        self._attempted: set[str] = set()
        # Search hits are held here and nowhere else: their single sink is the text
        # shown to the user. They are never written to slots, arguments or memory.
        self.search_hits: list = []
        self._resume_state: State | None = None

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

        self._last_question = ""
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

    def _progress(self, key: str, detail: str | None = None) -> None:
        """Announce what the agent is doing, for a human watching.

        Written to the trace as `progress` so the UI streams it from the same
        append-only log it already reads, rather than a second channel that can
        disagree with the evidence.
        """
        text = PROGRESS.get(key, key)
        if self.tracer:
            self.tracer.emit("progress", key=key, text=text, detail=detail,
                             state=self.session.state.value)

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

        self._progress("thinking")
        # The streamed prose is progress, not the terminal sentence: it is written to
        # the trace as `reply_delta` and shown as it arrives, while the sentence a user
        # is finally given still comes from render_outcome. A model that narrates one
        # thing and does another must not be able to leave the narration on screen
        # unopposed.
        def _on_delta(text: str) -> None:
            if self.tracer:
                self.tracer.emit("reply_delta", text=text, state=self.session.state.value)

        try:
            action = self.llm.propose(
                self.context.render_text(prompt),
                state=self.session.state.value,
                on_delta=_on_delta,
            )
        except ScriptExhausted:
            raise
        except LLMError:
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

    def _clarify(self, msg: str, *, repeat: bool = False) -> TurnResult:
        """Ask the user something and make the question visible.

        The question went only into history, so a watcher saw a turn end with no reply
        and no explanation — the agent appeared to give up silently. It is a `clarify`
        event for the same reason progress is: the trace is the record of what happened.
        """
        if self.session.clarify_turns >= MAX_CLARIFY_TURNS and not repeat:
            # Already asked once this turn. Do not ask again: leave with a sentence.
            missing = plain_slots(missing_slots(self.session.slots))
            self.session.slots["reason"] = (
                f"I asked once and did not get {missing or 'the details'} — say the word "
                "and I will pick it straight up"
            )
            self._terminal("aborted_missing_details")
            return self._result(None, None)
        if self.tracer:
            self.tracer.emit("clarify", text=msg, state=self.session.state.value)
        # An unanswered question invalidates the *result* we had, not the *question* we
        # were answering. The last tool result and candidates are dropped so they cannot
        # be re-shown as though they answered the new question; the established slots are
        # kept, because "which service?" must not be asked of a user who said "plumber"
        # two turns ago.
        self.last_tool_result = None
        self.candidates = []
        # One question per user turn, whatever the model does next. The counter is per
        # turn, not per call: exempting repeats let a single turn ask five times in a row
        # and still call it "one clarify".
        if not repeat:
            self.session.clarify_turns += 1
        if self.session.clarify_turns > MAX_CLARIFY_TURNS:
            # Say what was still missing. "Some details" is true and useless; the user
            # is owed the name of the thing the agent gave up waiting for.
            missing = plain_slots(missing_slots(self.session.slots))
            self.session.slots["reason"] = (
                f"I asked once and did not get {missing or 'the details'} — say the word "
                "and I will pick it straight up"
            )
            self._terminal("aborted_missing_details")
            return self._result(None, None)
        if self.session.state is State.NEW:
            self._transition(State.CLARIFYING)
        self.history.append("assistant", msg)
        return self._result(None, msg)

    def _ask_which(self, *, service: str | None, area: str | None,
                   what: str = "provider") -> str:
        """Ask for a choice, with the real choices in the question."""
        return ask_which(self.server, service=service, area=area, what=what)

    def _elsewhere(self, *, area: str | None, service: str | None) -> str:
        """What the world does serve, phrased as an offer the user can act on."""
        return elsewhere(self.server, area=area, service=service)

    _PROGRESS_FOR = {
        "search_providers": "searching",
        "get_provider": "provider",
        "hold_slot": "holding",
        "web_search": "research",
        "book": "executing",
        "cancel": "executing",
        "reschedule": "executing",
        "get_booking_status": "verifying",
        "list_bookings": "searching",
    }

    def _dispatch(self, action: Action) -> TurnResult:
        """Send an already-admitted action through the gateway."""
        name = action.tool or ""
        self._attribute(name, action.args)
        if key := self._PROGRESS_FOR.get(name):
            self._progress(key, detail=name)

        # A model repeating the *same call with the same arguments* is stuck. Keyed on
        # the arguments, not the tool name: inspecting two different providers is
        # progress. This is what the tool budget exists to bound, but the budget is
        # only checked at turn entry, so an intra-turn loop could never trip it and
        # surfaced as a `tool_failure` that never happened.
        fingerprint = json.dumps([name, action.args], sort_keys=True, default=str)
        if fingerprint in self._attempted:
            if self.tracer:
                self.tracer.emit("loop_detected", tool=name,
                                 state=self.session.state.value)
            # A repetition is its own outcome. Reporting it as a spent budget told the
            # user they had used calls they had not used, and named the wrong remedy.
            self._terminal("repeated_tool_call")
            self.session.slots["reason"] = f"repeated {name} with no change"
            return self._result(name, None)
        self._attempted.add(fingerprint)
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
        self.last_tool_result = summarise(name, result.value)
        if self.tracer:
            self.tracer.emit("tool_result_summary", tool=name,
                             chars=len(self.last_tool_result or ""))
        return self._after_tool(name, result.value)

    def _on_tool(self, action: Action) -> TurnResult:
        name = action.tool or ""
        assert action.tool is not None

        # --- a search for an area we do not serve is answered here, before the state
        # branches. In NEW the proposal is admitted as intent first, which persisted the
        # bad area into slots and turned a one-word shorthand into a dead end.
        if name == "search_providers" and action.args.get("area"):
            known = known_areas(self.server)
            resolved, candidates = resolve_area(action.args["area"], known)
            if resolved is None:
                options = ", ".join(candidates[:4])
                msg = (f'I do not have an area called "{action.args["area"]}". '
                       f"I serve {options}. Which one did you mean?"
                       if options else "Which area did you mean?")
                self._clarify(msg, repeat=(msg == self._last_question))
                self._last_question = msg
                return self._result(name, msg)
            if resolved != action.args["area"]:
                # Same action, spelled the way the provider table spells it.
                action.args["area"] = resolved

        # --- a read-only lookup is a detour from wherever the user is. Handled before
        # the per-state logic because it must be reachable from NEW, SELECTING and the
        # restore path alike, and because it never touches slots.
        if name == "web_search" and self.session.state is not State.RESEARCHING:
            return self._open_research(action)

        # --- NEW/CLARIFYING admit the model's structured proposal as its intent.
        # DISCOVERING is included because resolving a booking leaves the session there,
        # and the next legitimate step (holding the target slot) happens in SELECTING.
        if self.session.state in (State.NEW, State.CLARIFYING, State.DISCOVERING):
            return admit(self, action)

        # A search is legal from SELECTING too: after resolving a provider
        # ("the same electrician as last time") the next step is to look at their
        # slots, and a failed search retried with a wider area is the same move.
        # Retried from SEARCHING too: a user who names a different area mid-search is
        # asking for another search, not for the previous answer again. Without this the
        # old candidates were re-displayed under the new area name.
        if name == "search_providers" and self.session.state in (
                State.SELECTING, State.SEARCHING):
            if action.args.get("service") and action.args.get("area"):
                self.session.slots.setdefault("service", action.args["service"])
                self.session.slots.setdefault("area", action.args["area"])
                if self.session.state is State.SELECTING:
                    self._transition(State.SEARCHING)

        # --- side-effecting tools are dispatched only through the approval store.
        # A model proposing one with nothing pending is asking to *start* an
        # approval, not asking to execute: refuse only if there is nothing to act on.
        if name in ("book", "cancel", "reschedule"):
            if self.pending is None:
                return self._open_approval(action)
            return execute_approved(self, action)

        return self._dispatch(action)

    def _open_research(self, action: Action) -> TurnResult:
        """Enter RESEARCHING, remembering where the user was.

        A research detour must return to the booking flow it interrupted. The resume
        state is recorded rather than inferred, because inferring it from slots is how
        a "is this plumber any good?" question silently restarts the search.
        """
        if self.session.state is not State.RESEARCHING:
            resume = self.session.state
            # Returning to NEW would strand: NEW dispatches nothing, so the next turn
            # could only ask a question it already answered.
            self._resume_state = State.CLARIFYING if resume is State.NEW else resume
            self._transition(State.RESEARCHING)
        return self._dispatch(action)

    def _after_research(self, value) -> TurnResult:
        """Return to the interrupted flow, carrying the hits as display text."""
        self.search_hits = list(value or [])
        if self.search_hits:
            shown = "\n".join(hit.display() for hit in self.search_hits[:5])
            text = (
                "Here is what the web says — these are external pages, not our "
                "provider list, so treat them as claims rather than verified offers:\n"
                + shown
            )
        else:
            text = ("I searched but found nothing usable. That is a search result, "
                    "not a booking problem.")
        self.history.append("assistant", text)
        if self.tracer:
            self.tracer.emit("research_done", hits=len(self.search_hits),
                             resume=(self._resume_state.value if self._resume_state else None))
        # Back to whatever the user was doing. Never guessing: default to SELECTING
        # when there are candidates, otherwise CLARIFYING so the agent asks.
        target = self._resume_state or State.CLARIFYING
        self._resume_state = None
        if target is not State.RESEARCHING:
            self._transition(target)
        return self._result("web_search", text)

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
                ) if is_manage_request(self.session.slots.get("last_user", "")) else None
                self.session.slots["service"] = booking.service
                self.session.slots["area"] = booking.area
                # A resolved booking needs no search: the next step is to hold the
                # target slot, which is legal only in SELECTING.
                self._transition(State.SELECTING)
                return self._result(name, None)
            return self._result(name, None)

        if name == "search_providers":
            # Replace, never merge: the list belongs to the search that produced it, and
            # a stale candidate offered under a new area is a wrong answer that looks
            # authoritative.
            self.candidates = list(value)
            self.last_choice = None
            if not self.candidates:
                # An empty result must say what would not have been empty. "No provider
                # matches" sends a user back to re-guess; the areas we do serve, and the
                # services available in them, turn a dead end into one more question.
                area = self.session.slots.get("area")
                service = self.session.slots.get("service")
                elsewhere = self._elsewhere(area=area, service=service)
                self.session.slots["reason"] = (
                    f"No provider does {service} in {area} right now."
                    + (f" I can offer {elsewhere}." if elsewhere else
                       " Widening the day or the budget would give me more to show.")
                )
                self._terminal("no_match")
                return self._result(name, None)
            self._transition(State.SELECTING)
            return self._result(name, None)

        if name == "get_provider":
            self.last_choice = value
            return self._result(name, None)

        if name == "web_search":
            return self._after_research(value)

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
            return after_verify(self, value)

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
        return execute_approved(self, Action(self.pending.kind, {}, None))

    # ----------------------------------------------------------- reference resolve

    def _resolve_reference(self, bookings: list[Booking], utterance: str = ""):
        """G1: the ladder. Returns a booking dict, a question string, or None.

        The service is taken from the user's own words when the slots do not have it:
        "move my AC servicing" names ac_service even though nothing has been searched.
        Without this the ladder matched every confirmed booking and asked a question
        the script could not have anticipated.
        """
        wanted = self.session.slots.get("service") or service_from(utterance)
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
        replacement = find_replacement(
            self.server, self.memory, self.session.user_id, cancelled
        )

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
