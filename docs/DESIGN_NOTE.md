# Design note

**The model proposes, the runtime disposes.** Each guarantee below is enforced by
application code and provable from a trace, not by prompt wording.

## State, not conversation

An 11-state FSM persisted to SQLite after every transition. The session row is the
authority; the conversation is only an input. Restart reconciliation, single-field
correction and the context budget follow: a correction is a row update, and the loop
— not the model — decides what history is replayed. `DISCOVERING` exists
because two example requests name an existing booking; without a state whose legal
tool is `list_bookings`, cancel and reschedule work only in the session that created
the booking.

## Approval, enforced twice

Provider, service, slot, price and target are frozen and hashed into an approval row
with a TTL. Two genuinely independent blocks guard the side effect:

1. **Call-site monopoly** — `book`/`cancel`/`reschedule` are reachable only through
   `ApprovalStore.execute_if_approved`, enforced by an import-graph test;
   mutation-tested by appending a call to `watcher.py`.
2. **Runtime digest token** — the store refuses unless a pending row matches the live
   action's digest — even if a future path bypasses the approved function.

The grep catches a new code path, the token a new route through an existing one. A
price moving between search and hold is shown at hold time, so the trap is visible
before approval rather than an invisible mismatch after it. Reply classification is a
closed phrase list in code with negation-wins precedence; no model sits on the
approval path.

## Idempotency and ambiguous outcomes

`idempotency_key = uuid5(FIXED_NAMESPACE, f"errand/v1/{digest}")` — same action, same
key, across retries, repeated confirmation and restart; a different slot is a
different key. On timeout the agent never retries blind: query `get_booking_status`
by key; booked → report and store the id; not booked → one retry with the same key;
still unknown → a second query, then terminal `unverified` naming the reference. A
model sentence saying "booked" cannot produce a confirmed state — only a tool success
or an affirmative status query can.

## Memory and context

Fifty seeded rows, explicit and inferred distinct: an inferred fact starts
unconfirmed, and only confirmed rows are admissible however high they score. Forgetting is a cascade — the memory row is tombstoned, slots whose
provenance names it are unset and re-asked, and the conversation span is redacted. The
prompt is assembled under a hard 2,000-token budget (preamble 220, tools 180, memory
300, history 500, tool result 500, reserve 300). Every turn writes a manifest of
per-slice counts and included/excluded memory ids with reasons — ids only, never
values.

## Observability: prove it on the trace

Each run writes an event log of turns, context manifests, model actions, tool calls
with sanitized arguments and latency, side-effect outcomes and the terminal sentence.
Eleven invariants are predicates over that log: no side effect without a preceding
approval; at most one confirmed effect per key; a claimed success implies a persisted
record or an affirmative status query; no state-illegal tool call; budgets respected;
no tool argument carrying review text; every tool call attributed to a preceding model
action; no forgotten memory in a rendered prompt; the terminal sentence byte-identical
to its template. Attribution is what stops the suite passing against a hand-written
`switch` and leaving "a model drove this run" unevidenced.

## The trace, annotated

Scenario 09, a booking that times out after possibly succeeding:

```text
seq  9  approval_requested  digest=45966df12ed3  "…quoted Rs 1,400. Confirm?"
seq 11  action_attributed   book   state=EXECUTING       # approval row matched the live digest
seq 12  side_effect_error   book   error=timeout         # the call may have succeeded
seq 13  tool_call           get_booking_status(key=b34f…338) → booked, bk_0001
seq 14  side_effect_ok      book   verified=true         # success asserted only now
seq 15  terminal            booked_verified_after_timeout
```

No second `book` call: the key is a function of the digest, so a retry cannot
double-book.

## Numbers

| Quantity | Value |
|---|---|
| States | 11 |
| Terminal tokens | 14 |
| Scenarios | 45 (45 pass) |
| Invariants | 11 (100%) |
| Runtime dependencies | 0 |
| Source / test lines | 5,838 / 4,593 |
