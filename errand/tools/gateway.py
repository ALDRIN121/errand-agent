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


# Argument names each tool accepts, derived from the server so the contract cannot
# drift from the implementation.
def _signatures(server: MockToolServer) -> dict[str, set[str]]:
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
