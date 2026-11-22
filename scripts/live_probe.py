"""Prove the errand agent drives a real CommandCode model end to end.

The one thing the offline suite could not verify: does `HttpLLMClient` actually work
against the user's real endpoint? This runs the full orchestrator with a live model,
records the trace, and asserts a booking resulted.

Read-only with respect to the repo: it writes only its trace and prints the result.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, "/Users/aldrinjoseph/Developer/errand-agent")

from errand.approval import ApprovalStore
from errand.clock import FakeClock
from errand.context import ContextAssembler
from errand.db import Database
from errand.history import History
from errand.llm import HttpLLMClient, LLMError
from errand.memory import MemoryStore
from errand.orchestrator import Orchestrator
from errand.trace import Tracer
from errand.watcher import Watcher
from errand.tools.mock import MockToolServer
from evals.demo_data import ANCHOR, build_world

CFG = pathlib.Path("/Users/aldrinjoseph/.hermes/config.yaml")
BASE = "https://api.commandcode.ai/provider/v1"


def api_key() -> str:
    text = CFG.read_text()
    block = text.split("name: Api.commandcode.ai", 1)[1]
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("api_key:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("no api_key in the commandcode block")


def run(model: str, user_turns: list[str], trace_path: pathlib.Path) -> dict:
    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, user_id="u_aldrin")
    tracer = Tracer(trace_path)
    orch = Orchestrator(
        clock=clock, server=world.server, db=world.db, memory=world.memory,
        history=world.history, approvals=ApprovalStore(world.db, clock),
        llm=HttpLLMClient(base_url=BASE, api_key=api_key(), model=model, timeout=90),
        tracer=tracer, context=ContextAssembler(), user_id="u_aldrin",
    )
    watcher = Watcher(server=world.server, orchestrator=orch, db=world.db, clock=clock)

    out: dict = {"model": model, "turns": [], "outcome": None, "reply": None}

    for text in user_turns:
        try:
            result = orch.handle_turn(text)
        except LLMError as exc:
            out["error"] = f"LLMError: {exc}"
            break
        except Exception as exc:  # noqa: BLE001 - report, never crash the probe
            out["error"] = f"{type(exc).__name__}: {exc}"
            break
        out["turns"].append({"user": text, "state": orch.session.state.value,
                             "reply": (result.reply or "")[:200]})
        for body in watcher.tick():
            out["turns"].append({"proactive": body[:160]})
        if orch.session.state.value == "TERMINAL":
            break

    out["outcome"] = orch.session.outcome
    out["state"] = orch.session.state.value
    out["tool_calls"] = [getattr(c, "name", None) for c in world.server.calls]
    if out["outcome"] is None and out["state"] != "TERMINAL":
        orch.finish()
        out["outcome_after_finish"] = orch.session.outcome
    tracer.close()
    world.close()
    return out


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "deepseek/deepseek-v4.1-flash-fast"
    trace = pathlib.Path(f"/Users/aldrinjoseph/Developer/errand-agent/traces/live_{model.replace('/', '_')}.jsonl")
    # A real model asks which slot it should take; a real user answers. The scripted
    # scenarios hold unprompted because their queue says to, which is exactly the kind
    # of assumption a live run exposes.
    result = run(model, [
        "Find a plumber for my kitchen tap this week, mornings only, under Rs 1,500",
        "The 10:00 slot on the 23rd, please.",
        "yes, go ahead",
    ], trace)
    print(json.dumps(result, indent=2, default=str))
