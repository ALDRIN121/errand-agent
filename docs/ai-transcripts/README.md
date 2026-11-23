# AI transcripts

This directory holds the working transcript(s) with the AI assistant used to build
this project, as required by the brief.

## Files

| File | Contents |
|---|---|
| `session.md` | the original build: design, implementation, and the first defects found |
| `session-2.md` | the follow-up session: the defects found by driving the finished agent live |

`session-2.md` covers the work after the build was "done" — every defect in it was found
by using the agent, not by reading it: a crashing `list_bookings`, a cross-customer read,
a reload that came back to an empty chat over a live session, a finished session that
could not be typed into, and a `/` that did nothing. Those are the failures a test suite
written by the same person who wrote the code does not catch, which is why that session
ends with the agent driving its own UI in a browser.

## Regenerating

`session-2.md` is generated, not hand-edited:

```
PYTHONPATH=. .venv/bin/python -m docs.export_transcript <session-id-prefix> -o docs/ai-transcripts/session-2.md
PYTHONPATH=. .venv/bin/python -m docs.export_transcript --list   # find the id
```

The exporter redacts credentials on the way out — this repo is public, so redaction is a
property of the writer rather than a check performed once. `tests/test_transcript_export.py`
scans the shipped files for credential shapes.

## Format

The whole build ran in one session (51 user turns, ~1,640 assistant turns, ~1,640
tool calls). User and assistant text is verbatim, including the wrong turns; tool
calls are listed one line each with arguments elided for length; tool results are
omitted, since the traces under `traces/` reproduce them. Hermes' context-compaction
handoffs — summaries of earlier turns the runtime generated — are marked rather than
repeated.

## What the transcript shows

It is included unedited, including the parts where the assistant was wrong. Three
places are worth reading specifically:

1. **The fixture wrote bookings in areas their provider did not serve.** Three of
   five seeded bookings named a provider that did not offer that service in that
   area. Nothing forced the two tables to agree; the fix was cross-table assertions
   plus looking prices up instead of typing them.

2. **The transient retry was unreachable for side effects.** The one-retry rule lived
   in the gateway, but `book`/`cancel`/`reschedule` dispatch through the approval
   store and bypassed it, so a 503 on cancel went straight to `tool_failure`. Found by
   scenario 14, not by reading the code.

3. **A correction containing "not" killed a live approval.** "Make it mornings, not
   afternoons" classified as a denial. The classifier's constraint check only matched
   digits, so an amended day or window carried no signal.

There is also a stretch of roughly twenty turns where the assistant patched the
orchestrator repeatedly while the actual defect was in its own scenario scripts. The
lesson is recorded in the design note: a guard asserting the scripts were
answerable should have been written first, because it named the real fault
immediately once it existed.

## Why include the wrong turns

The brief asks for AI transcripts, and a transcript edited to show only correct
decisions would misrepresent both the failure modes and how they were caught. The
defects above are the evidence that the invariants do work: each was found by a
scenario or an architectural test, not by review.
