# Design note

**The model proposes, the runtime disposes.** Every guarantee below is enforced by
application code and provable from a trace. Nothing rests on prompt wording.

## The two independent blocks on side effects

Not offering a tool, refusing it at the gateway, and refusing it at the approval
store all read the same persisted state and digest, so one bad transition defeats
all three. The pair that is genuinely independent:

1. **Call-site monopoly** — `book`/`cancel`/`reschedule` are reachable only through
   `ApprovalStore.execute_if_approved`, enforced by an import-graph test
   (`tests/test_architecture.py`). A new caller fails the suite.
2. **Runtime digest token** — the store refuses the call unless a pending approval
   row matches the live action's digest and is inside its TTL, which holds even if
   some future path bypasses the approved function.

The grep catches a new code path; the token check catches a new route through an
existing one. Mutation-tested: appending `server.book(...)` to `watcher.py` fails the
suite and passes again when reverted.

## State, not conversation

A 10-state FSM persisted to SQLite after every transition. The session row is the
authority; the conversation is an input. That is what makes three things cheap:
restart reconciliation (reconcile non-terminal sessions through the status tool),
single-field correction (a row update, other fields byte-identical by construction),
and the context budget (the loop decides what history is replayed, not the model).

`DISCOVERING` exists because two of the three example requests name an existing
booking. Without a state whose legal tool is `list_bookings`, cancel and reschedule
are reachable only for a booking created in the same session — the case that is
easiest to demo and least likely in use.

## Approval is a row with a digest

Material details (provider, service, slot, price, target) are frozen into an
immutable object and hashed. The digest is recomputed immediately before the side
effect and must match. Consequences:

- A price that moves between search and hold is **shown to the user** at hold time,
  so the trap is visible before approval rather than becoming an invisible digest
  mismatch after it.
- Reply classification is a closed phrase list in code with negation-wins precedence.
  No model on the approval path: a model classifier routes approval through the same
  untrusted component it exists to constrain, and adds a paid call to the one turn
  that must be deterministic.
- A correction containing a negation ("mornings, not afternoons") is an amendment,
  not a refusal. This was a real bug: it once killed a live approval.

## Ambiguous outcomes

`idempotency_key = uuid5(FIXED_NAMESPACE, f"errand/v1/{digest}")` — same action, same
key, across retries, repeated confirmation and restart. A different slot is a
different key, correctly.

On timeout: never retry blind. Query `get_booking_status` by key. Booked → report
success and store the id. Not booked → one retry, same key. Still unknown → second
query, then terminal `unverified` carrying a reference. A model sentence saying
"booked" cannot produce a confirmed state; that state is written only by a tool
success or a status query that says booked.

## Untrusted text: containment, then detection

Review text is parsed into a typed field tagged untrusted with exactly one sink: the
text shown to the user. It has no path into slots, tool arguments, permissions or
memory, so a successful injection changes what the user reads and nothing else.
Pattern scanning exists for observability and is not the safety property — a regex is
not an enforcement boundary.

## The unattended path is rules, not an agent

A provider cancellation fires with no user turn. Picking a replacement inside known
preferences is a sort, not a judgement:

```text
rank = (-rating, price_paise, id)   # technician preference wins when set
```

A model there would add cost and nondeterminism to the one path no human watches. The
run records `llm_mode="none"`, so the absence of a model call is auditable.

## Prove on the trace, not on the reply

Eleven invariants, all predicates over the event log: no side effect without a
preceding approval; at most one confirmed effect per key; a claimed success implies a
persisted record or an affirmative status query; no state-illegal tool call; budgets
respected; no tool argument carrying review text; **every tool call attributed to a
preceding model action**; no traceback in a reply; no forgotten memory in a rendered
prompt; and the terminal sentence byte-identical to its template.

Action attribution is what stops the suite passing against a hand-written `switch`
statement and leaves "a model drove this run" unevidenced.

## Numbers

| Quantity | Value |
|---|---|
| States | 10 |
| Terminal tokens | 13 |
| Scenarios | 37 (37 pass) |
| Invariants | 11 (100%) |
| Runtime dependencies | 0 |
| Source / test lines | 3,557 / 2,709 |

## Where the scripted suite stops

`ScriptedLLM` replays a queue and ignores its prompt. That makes the runtime
properties provable — approval, idempotency, budgets, containment are all independent
of who proposes the action — but it means a prompt-shaped defect is invisible to all
37 scenarios. Three were: the state was not sent to the model, the last tool result
was hardcoded to `None`, and `hold_slot`'s `slot` argument was not expressible as
JSON.

So `tests/test_llm_http.py` runs the client against a real loopback `http.server` and
asserts on the bytes on the wire, and the full flow is demonstrated over HTTP:
search → the model reads the results → hold → approval → booked. The scripted suite
proves the *runtime*; the HTTP suite proves the *seam*.

## What I would do differently

Two scenario tables (fixture and provider list) describing the same world drifted:
three of five seeded bookings named providers that did not serve that service in that
area. Cross-table consistency is now asserted, and prices are looked up rather than
typed. Any hand-written fixture that duplicates a fact in two files needs a test that
compares them.
