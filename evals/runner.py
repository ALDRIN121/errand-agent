"""Run one scenario. This is the executable form of `evals/scenarios.py`.

`run_scenario` builds a fresh world, drives the turns, and returns everything the
invariant checks need: the trace events, the tool-call log, the session row and the
terminal sentence. No pytest here — the harness and any debugging session both use
this, so there is exactly one way a scenario gets executed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from errand.approval import ApprovalStore
from errand.clock import Clock, FakeClock
from errand.context import ContextAssembler
from errand.history import History
from errand.llm import Action, ScriptedLLM
from errand.memory import MemoryStore
from errand.orchestrator import Orchestrator, TurnResult
from errand.trace import Tracer, read_trace
from errand.watcher import Watcher
from errand.types import Slot
from evals.demo_data import ANCHOR, build_world
from evals.scenarios import Scenario


@dataclass
class RunResult:
    scenario_id: int
    outcome: str | None
    sentence: str | None
    turns: list[TurnResult] = field(default_factory=list)
    trace: list[dict] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    state: str = ""
    slots: dict = field(default_factory=dict)
    error: str | None = None

    def tools_named(self, name: str) -> int:
        return self.tool_calls.count(name)

    def events(self, kind: str) -> list[dict]:
        return [e for e in self.trace if e.get("type") == kind]


def _resolve_slot(world, days: int, hour: int) -> Slot:
    from datetime import timedelta

    start = ANCHOR + timedelta(days=days, hours=hour)
    return Slot(start=start, end=start + timedelta(hours=1), available=True)


def _to_action(spec: dict, world) -> Action:
    """Translate a scenario's intent-level args into a real tool call.

    Scenario files say `slot_day_offset`/`slot_hour` because they must be readable
    as data; the tools take a `Slot`. The translation lives here, once.
    """
    tool = spec.get("tool")
    args = dict(spec.get("args") or {})
    message = spec.get("message")


    if "slot_offset_days" in args:  # tolerate the historical spelling
        args["slot_day_offset"] = args.pop("slot_offset_days")
    if "slot_day_offset" in args or "slot_hour" in args:
        days = args.pop("slot_day_offset", 1)
        hour = args.pop("slot_hour", 9)
        slot = _resolve_slot(world, days, hour)
        if tool == "hold_slot":
            args["slot"] = slot
        elif tool == "reschedule":
            args["slot"] = slot
    return Action(tool=tool, args=args, message=message)


def actions_for(scenario: Scenario, world=None) -> list:
    """The scenario's scripted actions as `Action`s.

    Shared with the CLI's --script mode so a demo replays exactly the actions the
    eval asserts on, rather than a hand-written approximation of them.
    """
    flat: list[dict] = []
    for spec in scenario.scripted:
        if isinstance(spec, list):
            flat.extend(spec)
        else:
            flat.append(spec)
    return [_to_action(spec, world) for spec in flat]


def run_scenario(scenario: Scenario, trace_dir: Path | None = None) -> RunResult:
    clock = FakeClock(ANCHOR.replace(hour=8))
    # Offline by default. A scenario that wants search declares its results, and the
    # backend is a fixture rather than a socket: a suite that reaches the network is
    # not deterministic, and its failures stop meaning anything.
    web = None
    if not scenario.web_off:
        from errand.tools.websearch import MockWebSearch, hits_from

        web = MockWebSearch(hits=hits_from(scenario.web_hits),
                            failing=scenario.web_fails)
    world = build_world(clock, seed=scenario.seed, scenario=scenario.scenario,
                        user_id=scenario.user_id, web=web)
    trace_path = (trace_dir / f"scenario_{scenario.id:02d}.jsonl") if trace_dir else None
    tracer = Tracer(trace_path) if trace_path else None

    result = RunResult(scenario_id=scenario.id, outcome=None, sentence=None)
    try:
        # --- scenario mutations run before the first turn
        for cmd in scenario.mutations:
            _apply_mutation(cmd, world, clock)

        flat: list[dict] = []
        for spec in scenario.scripted:
            if isinstance(spec, list):  # several actions in one user turn
                flat.extend(spec)
            else:
                flat.append(spec)
        actions = [_to_action(s, world) for s in flat]
        llm = ScriptedLLM(actions)
        orch = Orchestrator(
            clock=clock,
            server=world.server,
            db=world.db,
            memory=world.memory,
            history=world.history,
            approvals=ApprovalStore(world.db, clock),
            llm=llm,
            tracer=tracer,
            context=ContextAssembler(),
            user_id=scenario.user_id,
        )

        watcher = Watcher(server=world.server, orchestrator=orch, db=world.db,
                          clock=clock, tracer=tracer)
        for body in watcher.tick():  # start-up tick: events raised while "down"
            if tracer:
                tracer.emit("proactive_delivered", body_chars=len(body))
        for body in watcher.pending():
            watcher.deliver(body["id"])

        restart_idx = None
        if scenario.restart_at:
            turn_part, idx_part = scenario.restart_at.split(":")
            restart_idx = int(turn_part)

        # a scripted advance_clock action runs before its turn, so TTL scenarios can
        # expire a hold mid-run rather than only before the session exists
        for i, text in enumerate(scenario.turns):
            if scenario.advance_before_turn and i == scenario.advance_before_turn[0]:
                clock.advance(float(scenario.advance_before_turn[1]))
                if tracer:
                    tracer.emit("clock_advanced", seconds=scenario.advance_before_turn[1])
            if restart_idx is not None and i == restart_idx:
                world.db.reconnect()
                orch = Orchestrator(
                    clock=clock, server=world.server, db=world.db,
                    memory=MemoryStore(world.db, clock, history=world.history),
                    history=world.history,
                    approvals=ApprovalStore(world.db, clock), llm=llm,
                    tracer=tracer, context=ContextAssembler(),
                    user_id=scenario.user_id, session_id="s_1",
                )
            result.turns.append(orch.handle_turn(text))
            for body in watcher.tick():
                if tracer:
                    tracer.emit("proactive_delivered", body_chars=len(body))
            for row in watcher.pending():
                watcher.deliver(row["id"])

        orch.finish()  # a session left alive at end of script is the user walking away
        result.outcome = orch.session.outcome
        result.sentence = result.turns[-1].sentence if result.turns else None
        result.state = orch.session.state.value
        result.slots = dict(orch.session.slots)
        result.tool_calls = [c.name for c in world.server.calls]
        result.prompts = list(llm.prompts)
    except Exception as exc:  # a scenario that explodes is a scenario that fails
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        if tracer:
            tracer.close()
        if trace_path and trace_path.exists():
            result.trace = read_trace(trace_path)
        elif tracer:
            result.trace = read_trace(tracer.path) if hasattr(tracer, "path") else []
        world.close()
    return result


def _apply_mutation(cmd: tuple, world, clock: FakeClock) -> None:
    kind = cmd[0]
    if kind == "forget":
        world.memory.forget("u_aldrin", cmd[1])
    elif kind == "correct":
        rows = [m for m in world.memory.admissible("u_aldrin") if m.key == cmd[1]]
        for m in rows:
            world.memory.update(m.id, cmd[2])
    elif kind == "seed_memories":
        import json
        from pathlib import Path as _P

        rows = json.loads((_P(__file__).resolve().parent / "fixtures" / "memories_50.json").read_text())
        for row in rows:
            if row.get("source") == "user" and not row.get("forgotten"):
                world.memory.add("u_aldrin", row["key"], row["value"], row.get("category", "misc"))
    elif kind == "advance_clock":
        clock.advance(float(cmd[1]))
    elif kind == "emit_cancel":
        world.server.emit_provider_cancel(cmd[1])
    else:
        raise ValueError(f"unknown mutation {kind}")
