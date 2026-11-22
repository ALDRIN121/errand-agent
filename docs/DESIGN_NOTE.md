# Design note

**The model proposes, the runtime disposes.** Every guarantee below is enforced by
application code and provable from a trace, not by prompt wording.

## The two independent blocks on side effects

Not offering a tool, refusing it at the gateway and refusing it at the approval store
all read the same persisted state, so a bad transition defeats all three. The pair
that is genuinely independent:

1. **Call-site monopoly** — `book`/`cancel`/`reschedule` are reachable only through
   `ApprovalStore.execute_if_approved`, enforced by an import-graph test
   (`tests/test_architecture.py`). A new caller fails the suite.
2. **Runtime digest token** — the store refuses the call unless a pending approval
   row matches the live action's digest and is inside its TTL, which holds even if
   some future path bypasses the approved function.

The grep catches a new code path; the token check catches a new route through an
existing one. Mutation-tested: `server.book(...)` appended to `watcher.py` fails the
suite and passes again when reverted.

## State, not conversation

An 11-state FSM persisted to SQLite after every transition. The session row is the
authority; the conversation is an input. That is what makes three things cheap:
restart reconciliation (reconcile non-terminal sessions through the status tool),
single-field correction (a row update, other fields byte-identical by construction),
and the context budget (the loop decides what history is replayed, not the model).

`DISCOVERING` exists because two of the three example requests name an existing
booking. Without a state whose legal tool is `list_bookings`, cancel and reschedule
are reachable only for a booking created in the same session — the case that is
easiest to demo and least likely in use.

## Approval is a row with a digest

Material details (provider, service, slot, price, target) are frozen and hashed; the
digest is recomputed before the side effect and must match.

- A price moving between search and hold is **shown to the user** at hold time, so the
  trap is visible before approval rather than an invisible digest mismatch after it.
- Reply classification is a closed phrase list in code with negation-wins precedence.
  No model on the approval path: a model classifier routes approval through the same
  untrusted component it exists to constrain, and adds a paid call to the one turn that
  must be deterministic.
- A correction containing a negation ("mornings, not afternoons") is an amendment, not
  a refusal. This was a real bug: it once killed a live approval.

## Ambiguous outcomes

`idempotency_key = uuid5(FIXED_NAMESPACE, f"errand/v1/{digest}")` — same action, same
key, across retries, repeated confirmation and restart. A different slot is a
different key, correctly.

On timeout: never retry blind. Query `get_booking_status` by key. Booked → report
success and store the id. Not booked → one retry, same key. Still unknown → second
query, then terminal `unverified` with a reference. A model sentence saying "booked"
cannot produce a confirmed state; only a tool success or an affirmative status query can.

## Untrusted text: containment, then detection

Review text is parsed into a typed field tagged untrusted with one sink: the text shown
to the user. It has no path into slots, tool arguments, permissions or memory, so an
injection changes what the user reads and nothing else. Pattern scanning is
observability, not the safety property.

## Unattended recovery is rules, not an agent

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

Action attribution is what stops the suite passing against a hand-written `switch` and
leaving "a model drove this run" unevidenced.

## Numbers

| Quantity | Value |
|---|---|
| States | 11 |
| Terminal tokens | 14 |
| Scenarios | 45 (45 pass) |
| Invariants | 11 (100%) |
| Runtime dependencies | 0 |
| Source / test lines | 5,275 / 3,814 |

## Web search is read-only, which is what makes it safe

`web_search` answers what the provider list cannot: costs, reputation, coverage. It
gets its own state (`RESEARCHING`) rather than widening `SEARCHING`, so the tool matrix
stays a spec.

Containment is structural. `SearchHit.trust` is `"untrusted"` and `__post_init__`
refuses anything else. Hits have one sink: the text shown to the user. No page text
reaches a slot, an argument or a memory row, so a page saying "book prov_06" changes what
the user reads and nothing else. `detect_injection` writes a trace flag; a regex is
observability, not a boundary. Research resumes where the user was — recorded, not
inferred — and `NEW` maps to `CLARIFYING` because `NEW` dispatches nothing.

Streaming obeys the same rule. The model's prose arrives as `reply_delta` and is removed
when the turn ends; the sentence a user is left with still comes from `render_outcome`,
so a streamed half-sentence cannot contradict the real outcome. The tool-call object is
never shown — half an Action is noise, not a half-finished decision.

## Where the scripted suite stops

`ScriptedLLM` replays a queue and ignores its prompt. That makes the runtime properties
provable — approval, idempotency, budgets and containment do not depend on who proposes
the action — but a prompt-shaped defect is invisible to every scenario. Three were: the
state was never sent, the last result was hardcoded to `None`, and `hold_slot`'s `slot`
was not expressible as JSON.

So `tests/test_llm_http.py` runs the client against a loopback `http.server` and asserts
on the bytes on the wire, and the flow is demonstrated over HTTP: search → the model reads the results → hold →
approval → booked. The scripted suite proves the *runtime*; the HTTP suite the *seam*.

## What I would do differently

Two tables describing the same world drifted: three of five seeded bookings named a
provider that did not serve that service in that area. Consistency is now asserted and
prices are looked up, not typed. A fixture that duplicates a fact in two files needs a
test comparing them.
