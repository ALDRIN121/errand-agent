"""Exercise the real HTTP transport against a real local server.

The scripted client is what the eval suite drives, so `HttpLLMClient` was the one
path with no coverage: typed, reviewed, and never actually sent a byte. These tests
start a genuine `http.server` on loopback and speak real HTTP to it, which closes
that gap without needing credentials.

They assert on the wire, not on a mock: request headers, the JSON body, and how each
class of failure is normalised. A mock of `urlopen` would prove the call happened; a
server proves the call was *correct*.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from errand.llm import Action, HttpLLMClient, LLMError


class _Recorder:
    """Shared state between the test and the server thread."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.reply: dict | None = None
        self.status = 200
        self.delay = 0.0
        self.raw_body: str | None = None


def _make_server(rec: _Recorder) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8")
            rec.requests.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": json.loads(body) if body else {},
                }
            )
            if rec.delay:
                time.sleep(rec.delay)
            if rec.status != 200:
                payload = b'{"error": "boom"}'
                self.send_response(rec.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if rec.raw_body is not None:
                payload = rec.raw_body.encode("utf-8")
            else:
                payload = json.dumps(rec.reply or {}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass  # keep pytest output clean

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


@pytest.fixture
def server():
    rec = _Recorder()
    httpd = _make_server(rec)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield rec, f"http://{host}:{port}", httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _offered_tools(system: str) -> set[str]:
    """Parse the tool list out of the system prompt.

    The label shares a line with the state, so the split is on the label itself.
    """
    marker = "Tools available to you:"
    assert marker in system, system
    tail = system.split(marker, 1)[1]
    tail = tail.split("\n", 1)[0]
    return {name.strip(" .") for name in tail.split(",") if name.strip(" .")}


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_a_real_request_is_sent_and_the_action_comes_back(server):
    """The path that had never run: build a request, send it, parse the reply."""
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": "search_providers", '
                            '"args": {"service": "plumber", "area": "Koramangala"}, '
                            '"message": null}')

    client = HttpLLMClient(base_url=base_url, api_key="test-key", model="m1")
    action = client.propose("plumber in Koramangala this week", state="SEARCHING")

    assert action.tool == "search_providers"
    assert action.args == {"service": "plumber", "area": "Koramangala"}
    assert rec.requests, "the server never received a request"


def test_the_request_carries_the_contract_and_the_auth_header(server):
    """A call that arrives without these is a call the real endpoint would reject."""
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": null, "args": {}, "message": "hi"}')

    client = HttpLLMClient(base_url=base_url, api_key="sk-secret", model="my-model")
    client.propose("hello", state="NEW")

    req = rec.requests[0]
    assert req["path"] == "/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer sk-secret"
    assert req["headers"]["Content-Type"].startswith("application/json")
    assert req["body"]["model"] == "my-model"
    assert req["body"]["temperature"] == 0.0
    assert req["body"]["response_format"] == {"type": "json_object"}
    roles = [m["role"] for m in req["body"]["messages"]]
    assert roles == ["system", "user"]


def test_the_state_and_its_legal_tools_reach_the_model(server):
    """A client that withholds the state makes a real model guess at legality."""
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": null, "args": {}, "message": "ok"}')

    HttpLLMClient(base_url=base_url, api_key="k", model="m").propose("x", state="SEARCHING")

    system = rec.requests[0]["body"]["messages"][0]["content"]
    assert "SEARCHING" in system
    tools_line = next(
        line for line in system.splitlines() if "Tools available to you:" in line
    )
    offered = _offered_tools(system)
    assert "search_providers" in offered


def test_new_state_offers_the_tools_it_can_propose(server):
    """NEW dispatches nothing, but a proposal carrying the slots is how it leaves NEW.

    A prompt that said "no tools" was truthful about dispatch and useless to the
    model: it could only ask a question it did not need to ask.
    """
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": "search_providers", "args": {"service": "plumber", '
                            '"area": "Koramangala", "date_range": {"start": "2026-11-24", '
                            '"end": "2026-11-29"}}, "message": null}')
    HttpLLMClient(base_url=base_url, api_key="k", model="m").propose("x", state="NEW")
    system = rec.requests[0]["body"]["messages"][0]["content"]
    assert "NEW" in system
    tools_line = next(l for l in system.splitlines() if "Tools available to you:" in l)
    assert "search_providers" in tools_line
    assert "propose" in system.lower(), "the model must be told a proposal is how it starts"


def test_new_state_does_not_offer_side_effects(server):
    """Proposable is wider than dispatchable, but never that wide."""
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": null, "args": {}, "message": "ok"}')
    HttpLLMClient(base_url=base_url, api_key="k", model="m").propose("x", state="NEW")
    system = rec.requests[0]["body"]["messages"][0]["content"]
    # compare exact tool names: "list_bookings" contains "book" as a substring
    offered = _offered_tools(system)
    forbidden = {"book", "cancel", "reschedule", "hold_slot"}
    assert not (offered & forbidden), offered


def test_an_unrecognised_state_name_does_not_crash(server):
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": null, "args": {}, "message": "ok"}')
    client = HttpLLMClient(base_url=base_url, api_key="k", model="m")
    assert client.propose("x", state="NOT_A_STATE").tool is None


def test_the_prompt_is_sent_verbatim(server):
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": null, "args": {}, "message": "ok"}')
    HttpLLMClient(base_url=base_url, api_key="k", model="m").propose(
        "EXACT-PROMPT-TEXT", state="NEW"
    )
    assert rec.requests[0]["body"]["messages"][1]["content"] == "EXACT-PROMPT-TEXT"


def test_a_message_only_reply_is_not_a_tool_call(server):
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": null, "args": {}, "message": "Which area?"}')
    action = HttpLLMClient(base_url=base_url, api_key="k", model="m").propose("x", state="NEW")
    assert action.tool is None
    assert action.message == "Which area?"
    assert not action.is_tool_call


def test_an_unknown_tool_name_is_rejected_not_coerced(server):
    """The model's own output is validated like any other untrusted input."""
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": "drop_database", "args": {}, "message": null}')
    with pytest.raises(LLMError, match="malformed reply"):
        HttpLLMClient(base_url=base_url, api_key="k", model="m").propose("x", state="NEW")


def test_a_500_becomes_an_llm_error(server):
    rec, base_url, _ = server
    rec.status = 500
    with pytest.raises(LLMError, match="HTTP 500"):
        HttpLLMClient(base_url=base_url, api_key="k", model="m").propose("x", state="NEW")


def test_a_401_becomes_an_llm_error(server):
    rec, base_url, _ = server
    rec.status = 401
    with pytest.raises(LLMError, match="HTTP 401"):
        HttpLLMClient(base_url=base_url, api_key="k", model="m").propose("x", state="NEW")


def test_non_json_content_is_an_llm_error(server):
    """A model that replies with prose must not crash the run."""
    rec, base_url, _ = server
    rec.reply = _completion("Sure! I booked it for you.")
    with pytest.raises(LLMError, match="malformed reply"):
        HttpLLMClient(base_url=base_url, api_key="k", model="m").propose("x", state="NEW")


def test_a_json_reply_with_no_choices_is_an_llm_error(server):
    rec, base_url, _ = server
    rec.reply = {"error": {"message": "no choices"}}
    with pytest.raises(LLMError, match="malformed reply"):
        HttpLLMClient(base_url=base_url, api_key="k", model="m").propose("x", state="NEW")


def test_a_slow_server_times_out_as_an_llm_error(server):
    rec, base_url, _ = server
    rec.delay = 1.5
    client = HttpLLMClient(base_url=base_url, api_key="k", model="m", timeout=0.3)
    with pytest.raises(LLMError):
        client.propose("x", state="NEW")


def test_an_unreachable_host_is_an_llm_error():
    """Port 1 on loopback refuses immediately: no network dependency in the test."""
    client = HttpLLMClient(base_url="http://127.0.0.1:1", api_key="k", model="m",
                           timeout=1.0)
    with pytest.raises(LLMError):
        client.propose("x", state="NEW")


def test_a_trailing_slash_in_the_base_url_does_not_double_up(server):
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": null, "args": {}, "message": null}')
    HttpLLMClient(base_url=base_url + "/", api_key="k", model="m").propose("x", state="NEW")
    assert rec.requests[0]["path"] == "/chat/completions"


def test_side_effecting_tools_are_constructible_as_actions(server):
    """A real model may legitimately propose `book`; the runtime is what gates it."""
    rec, base_url, _ = server
    rec.reply = _completion('{"tool": "book", "args": {}, "message": null}')
    action = HttpLLMClient(base_url=base_url, api_key="k", model="m").propose("x", state="EXECUTING")
    assert action.tool == "book"
    assert action.tool in ("book", "cancel", "reschedule")


# ---------------------------------------------------------------- orchestrator tie-in


def test_an_llm_failure_ends_the_run_with_a_token_not_a_traceback():
    """I9 at the seam: an unreachable model must degrade to a stated outcome."""
    from errand.approval import ApprovalStore
    from errand.clock import FakeClock
    from errand.context import ContextAssembler
    from errand.orchestrator import Orchestrator
    from errand.policy import State
    from evals.demo_data import ANCHOR, build_world

    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, user_id="u_aldrin")
    dead = HttpLLMClient(base_url="http://127.0.0.1:1", api_key="k", model="m", timeout=1.0)
    orch = Orchestrator(
        clock=clock, server=world.server, db=world.db, memory=world.memory,
        history=world.history, approvals=ApprovalStore(world.db, clock), llm=dead,
        context=ContextAssembler(), user_id="u_aldrin",
    )
    result = orch.handle_turn("Find a plumber in Koramangala this week")

    assert orch.session.state is State.TERMINAL
    assert result.outcome == "tool_failure", result.outcome
    assert "Traceback" not in (result.reply or "")
    assert "LLMError" not in (result.reply or "")
    world.close()


# ------------------------------------------------- the whole loop over real HTTP


def test_the_search_result_reaches_the_model_on_the_next_step():
    """The defect this closes: `tool_result` was hardcoded to None.

    `ScriptedLLM` replays a queue and ignores the prompt, so the suite passed while a
    real model was asked to choose a provider it had never been told about.
    """
    rec = _Recorder()
    rec.reply = _completion(
        '{"tool": "search_providers", "args": {"service": "plumber", '
        '"area": "Koramangala", "date_range": {"start": "2026-11-24", '
        '"end": "2026-11-29"}}, "message": null}'
    )
    httpd = _make_server(rec)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address

    from errand.approval import ApprovalStore
    from errand.clock import FakeClock
    from errand.context import ContextAssembler
    from errand.llm import HttpLLMClient
    from errand.orchestrator import Orchestrator
    from evals.demo_data import ANCHOR, build_world

    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, user_id="u_aldrin")
    orch = Orchestrator(
        clock=clock, server=world.server, db=world.db, memory=world.memory,
        history=world.history, approvals=ApprovalStore(world.db, clock),
        llm=HttpLLMClient(base_url=f"http://{host}:{port}", api_key="k", model="m"),
        context=ContextAssembler(), user_id="u_aldrin",
    )
    orch.handle_turn("Find a plumber in Koramangala this week")
    httpd.shutdown()
    httpd.server_close()

    assert len(rec.requests) >= 2, "the loop must take a second step to use the result"
    second = rec.requests[1]["body"]["messages"]
    text = "\n".join(m["content"] for m in second)
    assert "Providers found" in text or "prov_" in text, text[-400:]
    assert orch.last_tool_result is not None
    world.close()


def test_the_last_tool_result_is_summarised_not_dumped():
    """The budget allows 500 tokens for a result; the raw payload is far larger."""
    from errand.clock import FakeClock
    from errand.orchestrator import _summarise
    from evals.demo_data import ANCHOR, build_world

    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, user_id="u_aldrin")
    providers = world.server.search_providers("plumber", "Koramangala")
    summary = _summarise("search_providers", providers)
    assert summary and "prov_06" in summary
    assert "rating" in summary and "Rs" in summary
    # the raw dataclass repr must not leak into the prompt
    assert "Provider(" not in summary
    assert len(summary) < 1200, len(summary)
    world.close()


def test_no_providers_summarises_to_a_statement():
    from errand.orchestrator import _summarise

    assert _summarise("search_providers", []) == "No providers matched."


def test_an_unknown_tool_summarises_to_nothing():
    from errand.orchestrator import _summarise

    assert _summarise("hold_slot", {"hold_id": "h1"}) is None


# ------------------------------------------------ the contract must be JSON-shaped


def test_hold_slot_accepts_a_json_slot_object():
    """A model speaks JSON; `hold_slot` declares `slot: Slot`.

    Without coercion the only way to hold a slot was to construct a Python object, so
    a real client could not use the contract at all — and the scripted suite hid it by
    passing the object straight in.
    """
    from errand.clock import FakeClock
    from errand.policy import State
    from errand.tools.gateway import Gateway
    from errand.tools.mock import MockToolServer
    from evals.demo_data import ANCHOR

    clock = FakeClock(ANCHOR.replace(hour=8))
    server = MockToolServer(clock, seed=7)
    gateway = Gateway(server=server)
    free = next(s for s in server.get_provider("prov_06").slots if s.available)

    result = gateway.call(State.SELECTING, "hold_slot", {
        "provider_id": "prov_06",
        "slot": {"start": free.start.isoformat(), "end": free.end.isoformat()},
    })

    assert result.ok, result.error
    assert result.value.hold_id.startswith("hold_")
    assert result.value.quote_paise > 0


def test_hold_slot_accepts_a_bare_iso_string():
    from errand.clock import FakeClock
    from errand.policy import State
    from errand.tools.gateway import Gateway
    from errand.tools.mock import MockToolServer
    from evals.demo_data import ANCHOR

    clock = FakeClock(ANCHOR.replace(hour=8))
    server = MockToolServer(clock, seed=7)
    free = next(s for s in server.get_provider("prov_06").slots if s.available)
    result = Gateway(server=server).call(
        State.SELECTING, "hold_slot",
        {"provider_id": "prov_06", "slot": free.start.isoformat()},
    )
    assert result.ok, result.error


def test_a_malformed_slot_is_a_validation_refusal_not_a_crash():
    """I9: no traceback reaches the user, so coercion must fail structurally."""
    from errand.clock import FakeClock
    from errand.policy import State
    from errand.tools.gateway import Gateway
    from errand.tools.mock import MockToolServer
    from evals.demo_data import ANCHOR

    clock = FakeClock(ANCHOR.replace(hour=8))
    server = MockToolServer(clock, seed=7)
    result = Gateway(server=server).call(
        State.SELECTING, "hold_slot",
        {"provider_id": "prov_06", "slot": {"start": "not-a-timestamp"}},
    )
    assert not result.ok
    assert result.error.error_class == "validation"


def test_coercion_does_not_disturb_other_tools():
    from errand.clock import FakeClock
    from errand.policy import State
    from errand.tools.gateway import Gateway
    from errand.tools.mock import MockToolServer
    from evals.demo_data import ANCHOR

    clock = FakeClock(ANCHOR.replace(hour=8))
    server = MockToolServer(clock, seed=7)
    result = Gateway(server=server).call(
        State.SEARCHING, "search_providers",
        {"service": "plumber", "area": "Koramangala"},
    )
    assert result.ok and result.value
