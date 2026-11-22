"""The REPL.

Deliberately thin. Every terminal sentence comes from `render_outcome`; this module
never formats one, and `tests/test_cli_smoke.py` fails if an outcome literal appears
here. The reason is I3b: if the CLI can write its own final sentence, the
byte-stability guarantee is only as good as the CLI author's memory.

`--llm real` needs `ERRAND_LLM_BASE_URL`, `ERRAND_LLM_KEY` and `ERRAND_LLM_MODEL`.
Without them the CLI refuses rather than silently falling back to the script, because
a demo believed to be live but running a script is worse than an error.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from errand.approval import ApprovalStore
from errand.clock import FakeClock, RealClock
from errand.context import ContextAssembler
from errand.db import Database
from errand.history import History
from errand.llm import HttpLLMClient, ScriptedLLM
from errand.memory import MemoryStore
from errand.orchestrator import Orchestrator
from errand.outcomes import render_outcome
from errand.tools.mock import MockToolServer
from errand.trace import Tracer
from errand.watcher import Watcher
from evals.demo_data import ANCHOR, build_world

HELP = """commands:
  /memory            what I remember about you
  /forget <key>      forget a stored preference
  /state             the session state and slots
  /trace             path to this session's trace
  simulate cancel <booking_id>   raise a provider cancellation
  /quit              leave
"""


def build(args) -> tuple[Orchestrator, Watcher, Tracer | None, Database]:
    trace_path = Path(args.trace) if args.trace else None
    tracer = Tracer(trace_path) if trace_path else None

    clock = FakeClock(ANCHOR.replace(hour=8)) if args.seed is not None else RealClock()
    world = build_world(clock, seed=args.seed or 7, scenario=args.scenario,
                        user_id=args.user) if args.seed is not None else None
    if world is None:
        db = Database(args.db or ":memory:")
        server = MockToolServer(clock, seed=7, scenario=args.scenario)
        history = History()
        memory = MemoryStore(db, clock, history=history)
    else:
        db = world.db
        server = world.server
        history = world.history
        memory = world.memory

    if args.script is not None:
        args.llm = "scripted"

    if args.llm == "real":
        missing = [
            name for name in ("ERRAND_LLM_BASE_URL", "ERRAND_LLM_KEY", "ERRAND_LLM_MODEL")
            if not os.environ.get(name)
        ]
        if missing:
            raise SystemExit(
                "--llm real needs " + ", ".join(missing) + ". Refusing to fall back "
                "to --llm scripted silently."
            )
        llm = HttpLLMClient(
            base_url=os.environ["ERRAND_LLM_BASE_URL"],
            api_key=os.environ["ERRAND_LLM_KEY"],
            model=os.environ["ERRAND_LLM_MODEL"],
        )
    elif args.script is not None:
        # Demo without credentials: replay one scenario's actions. The queue is real
        # (it comes from evals/scenarios.py), so this is not an empty client.
        from evals.runner import actions_for
        from evals.scenarios import SCENARIOS

        chosen = next((s for s in SCENARIOS if s.id == args.script), None)
        if chosen is None:
            raise SystemExit(f"no scenario {args.script}; try 1-37")
        llm = ScriptedLLM(actions_for(chosen))
    else:
        # An empty script would make every turn silently produce nothing: the REPL
        # would look broken with no error. The scripted client exists for the eval
        # harness and for --script, both of which supply a real queue.
        raise SystemExit(
            "--llm scripted needs a queue: pass --script <scenario id>, or use "
            "--llm real with ERRAND_LLM_BASE_URL/KEY/MODEL set."
        )

    orch = Orchestrator(
        clock=clock, server=server, db=db, memory=memory, history=history,
        approvals=ApprovalStore(db, clock), llm=llm, tracer=tracer,
        context=ContextAssembler(), user_id=args.user,
    )
    watcher = Watcher(server=server, orchestrator=orch, db=db, clock=clock, tracer=tracer)
    return orch, watcher, tracer, db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="errand", description="home-services agent")
    parser.add_argument("--llm", choices=("real", "scripted"), default="real")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--scenario", default="happy")
    parser.add_argument("--user", default="u_aldrin")
    parser.add_argument("--trace", default=None)
    parser.add_argument("--db", default=":memory:")
    parser.add_argument("--script", type=int, default=None,
                        help="replay scenario N's model actions (no credentials needed)")
    args = parser.parse_args(argv)

    try:
        orch, watcher, tracer, db = build(args)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    # an event raised while the process was down must still fire
    for body in watcher.tick():
        print(f"\n[proactive] {body}")

    print("errand agent. /help for commands.")
    try:
        while True:
            try:
                line = input("\nyou> ").strip()
            except EOFError:
                break
            if not line:
                continue

            if _command(line, orch, watcher, tracer, db):
                continue

            result = orch.handle_turn(line)
            if result.reply:
                print(f"\nagent> {result.reply}")
            for body in watcher.tick():
                print(f"\n[proactive] {body}")
            if result.state.value == "TERMINAL":
                print("\n(session ended)")
                break
    finally:
        if tracer:
            tracer.close()
        db.close()
    return 0


def _command(line: str, orch, watcher, tracer, db) -> bool:
    if not line.startswith("/") and not line.startswith("simulate"):
        return False

    if line in ("/quit", "/exit"):
        raise SystemExit(0)

    if line == "/help":
        print(HELP)
        return True

    if line == "/memory":
        rows = orch.memory.admissible(orch.session.user_id)
        for m in rows:
            print(f"  {m.key:28s} {m.value}  ({m.source})")
        if not rows:
            print("  (nothing stored)")
        return True

    if line.startswith("/forget "):
        key = line.split(maxsplit=1)[1].strip()
        n = orch.memory.forget(orch.session.user_id, key)
        print(f"  forgot {n} row(s) for {key!r}")
        return True

    if line == "/state":
        print(f"  state:  {orch.session.state.value}")
        print(f"  slots:  {orch.session.slots}")
        print(f"  budget: turns={orch.session.turns} tools={orch.session.tool_calls}")
        return True

    if line == "/trace":
        print(f"  {tracer.path if tracer else 'no trace (pass --trace <path>)'}")
        return True

    if line.startswith("simulate cancel "):
        booking_id = line.split()[-1]
        orch.server.emit_provider_cancel(booking_id)
        for body in watcher.tick():
            print(f"\n[proactive] {body}")
        return True

    print("  unknown command. /help for the list.")
    return True


__all__ = ["main", "build", "render_outcome"]


if __name__ == "__main__":
    raise SystemExit(main())
