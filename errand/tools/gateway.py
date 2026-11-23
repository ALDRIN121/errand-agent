"""The one door between the model and the tools.

Every dispatch goes through `Gateway.call`. That is what makes the following true
by construction rather than by good behaviour:

* a tool the current state does not permit is refused before it reaches the server;
* arguments are validated against the contract, so a model cannot smuggle an
  unexpected key into a tool;
* every raw exception becomes a `ToolError` with a class, so no code path above
  this one needs a bare `except`;
* exactly one retry for `transient`, and none for anything else;
* every attempt is traced, including the refused ones.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from errand.policy import PolicyViolation, State, assert_tool_allowed
from errand.tools.mock import MockToolServer
from errand.trace import Tracer
from errand.types import Slot, ToolError


# Arguments the server needs but that are not the model's to supply. Which user a
# request belongs to is the session's fact, never the model's: it is removed from the
# model-facing contract before the unexpected-key check and injected at dispatch, so
# `{}` and `{"user_id": "someone_else"}` are indistinguishable to the server. Without
# this, `list_bookings` crashed on the empty call the prompt asks for and leaked
# another customer's bookings on the one it does not.
SESSION_BOUND = frozenset({"user_id"})


def _signatures(server: MockToolServer) -> dict[str, set[str]]:
    """Every argument each tool declares, `self` excluded.

    Bound keys are included so the server can be called completely; they are filtered
    out of what a model may send at the call site, never dropped from the server's
    own signature.
    """
    out: dict[str, set[str]] = {}
    for name in (
        "search_providers", "get_provider", "hold_slot", "book",
        "get_booking_status", "cancel", "reschedule", "list_bookings", "web_search",
    ):
        fn = getattr(server, name)
        params = inspect.signature(fn).parameters
        out[name] = {p for p in params if p != "self"}
    return out


RETRYABLE = frozenset({"transient"})


@dataclass
class CallResult:
    name: str
    args: dict
    value: Any = None
    error: ToolError | None = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.error is None


def _parse_dt(value):
    """Accept an ISO string, a datetime, or a dict carrying `start`/`end`."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return None


def _coerce(name: str, args: dict) -> dict:
    """Turn JSON-shaped values into the types the tool declares.

    A model speaks JSON; `hold_slot` declares `slot: Slot`. Without this the only way
    to hold a slot was to construct a Python object, which meant the contract could
    not be exercised by a real client at all — the scripted suite hid it by handing in
    the object directly.
    """
    if name != "hold_slot":
        return args
    raw = args.get("slot")
    if isinstance(raw, Slot) or raw is None:
        return args
    if isinstance(raw, dict):
        start = _parse_dt(raw.get("start"))
        end = _parse_dt(raw.get("end") or raw.get("start"))
        if start is None:
            raise ToolError("validation", "slot.start must be an ISO timestamp")
        return {**args, "slot": Slot(start=start, end=end or start)}
    start = _parse_dt(raw)
    if start is None:
        raise ToolError("validation", f"slot must be a Slot or ISO timestamp, got {type(raw).__name__}")
    return {**args, "slot": Slot(start=start, end=start)}


@dataclass
class Gateway:
    server: MockToolServer
    tracer: Tracer | None = None
    # The caller's identity, supplied by the session owner rather than by the model.
    # `list_bookings` filters on it; a model cannot change it, so it cannot read
    # another customer's bookings even if it names one.
    user_id: str = "u_local"
    calls_made: int = 0
    _sigs: dict[str, set[str]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._sigs = _signatures(self.server)

    # ------------------------------------------------------------------ dispatch

    def call(
        self,
        state: State,
        name: str,
        args: dict,
        *,
        approval_kind: str | None = None,
        transport: Callable[[], Any] | None = None,
    ) -> CallResult:
        """Dispatch one tool call, guarded.

        `transport` lets the approval store own the side-effecting call: the gateway
        checks legality and shape, then hands the actual invocation to the caller's
        approved closure, so `book` is never reachable from here without one.
        """
        self.calls_made += 1

        try:
            assert_tool_allowed(state, name, approval_kind)
        except PolicyViolation as exc:
            self._trace("tool_refused", name=name, args=args, reason=str(exc), state=state)
            return CallResult(name, args, error=ToolError("validation", str(exc)))

        allowed = self._sigs.get(name)
        if allowed is None:
            return CallResult(name, args, error=ToolError("validation", f"unknown tool {name}"))

        # Bound keys are never the model's to pass: anything it sends under one is
        # dropped rather than forwarded, so a call that names another customer is
        # answered for this session's customer, not refused and not honoured.
        args = {k: v for k, v in args.items() if k not in SESSION_BOUND}

        unexpected = set(args) - allowed
        if unexpected:
            detail = f"unexpected argument(s) {sorted(unexpected)} for {name}"
            self._trace("tool_refused", name=name, args=args, reason=detail, state=state)
            return CallResult(name, args, error=ToolError("validation", detail))

        # Coercion validates too, so a bad value is a structured refusal rather than an
        # exception escaping the gateway: I9 says no traceback reaches the user.
        try:
            args = _coerce(name, args)
        except ToolError as exc:
            self._trace("tool_refused", name=name, args=args, reason=exc.detail, state=state)
            return CallResult(name, args, error=exc)
        except Exception as exc:  # malformed ISO string, wrong type, anything else
            detail = f"could not interpret arguments for {name}: {exc}"
            self._trace("tool_refused", name=name, args=args, reason=detail, state=state)
            return CallResult(name, args, error=ToolError("validation", detail))

        fn = transport if transport is not None else getattr(self.server, name)
        # Only the server's own methods take **the declared kwargs**; a transport
        # closure takes none, because it already captured them.
        call_kwargs = {} if transport is not None else dict(args)
        # Identity is injected here, after the model's arguments have been checked and
        # coerced, so a tool that needs it gets the session's value and nothing else.
        if transport is None:
            call_kwargs.update({k: self.user_id for k in allowed & SESSION_BOUND})

        attempts = 0
        max_attempts = 2  # one retry, for transient only
        while True:
            attempts += 1
            try:
                value = fn() if transport is not None else fn(**call_kwargs)
            except ToolError as exc:
                if exc.error_class in RETRYABLE and attempts < max_attempts:
                    self._trace("tool_retry", name=name, args=args,
                                error=exc.error_class, attempt=attempts, state=state)
                    continue
                self._trace("tool_error", name=name, args=args,
                            error=exc.error_class, detail=exc.detail,
                            attempts=attempts, state=state)
                return CallResult(name, args, error=exc, attempts=attempts)
            except Exception as exc:  # noqa: BLE001 - this is the normalising boundary
                normalised = ToolError("unexpected", f"{type(exc).__name__}: {exc}")
                self._trace("tool_error", name=name, args=args,
                            error="unexpected", detail=str(exc), attempts=attempts, state=state)
                return CallResult(name, args, error=normalised, attempts=attempts)

            self._trace("tool_call", name=name, args=args,
                        attempts=attempts, state=state,
                        result=_summarise(value))
            return CallResult(name, args, value=value, attempts=attempts)

    # --------------------------------------------------------------------- trace

    def _trace(self, kind: str, **fields: Any) -> None:
        if self.tracer is None:
            return
        payload = {k: (v.value if isinstance(v, State) else v) for k, v in fields.items()}
        self.tracer.emit(kind, **payload)


def _summarise(value: Any) -> Any:
    """A small, JSON-safe projection for the trace.

    Full provider records would put every slot of every hit into the trace file; the
    trace needs to prove which tool ran and roughly on what, not replay the data.
    """
    if isinstance(value, list):
        return [_summarise(v) for v in value[:5]]
    if isinstance(value, dict):
        return {k: _summarise(v) for k, v in list(value.items())[:8]}
    for attr in ("booking_id", "id", "hold_id"):
        got = getattr(value, attr, None)
        if got is not None:
            return {attr: got, "status": getattr(value, "status", None)}
    if hasattr(value, "start") and hasattr(value, "end"):
        return {"start": value.start.isoformat(), "end": value.end.isoformat()}
    return value
