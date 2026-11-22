# Errand Agent

A chat agent that arranges home-service errands against a deliberately unreliable
mock provider environment. Built as a worked example of *the model proposes, the
runtime disposes*: the model chooses among actions the current state permits, and
application code decides whether that action is allowed and whether it actually
succeeded.

## Status

Under construction, phase by phase, per `docs/plans/errand-agent-plan.md`.

- `docs/designs/errand-agent.md` — design specification
- `docs/plans/errand-agent-plan.md` — implementation plan (phase gates)
- `.egm/reviews/errand-agent-r1/consolidated_review_report.md` — review findings

## Run

```sh
python -m pytest -q          # tests + eval scenarios
python -m errand             # interactive chat
```

## What it guarantees

- No booking, cancellation or reschedule without an approval whose digest matches
  the live material details.
- One side effect per approved action, across repeated confirmations, retries and
  process restarts.
- A booking is never reported as successful until `get_booking_status` or the
  booking call itself says so.
- Review text is data: it can change what the user reads, never what the agent may do.
- Forgotten and unconfirmed-inferred facts never reach the model's context.
