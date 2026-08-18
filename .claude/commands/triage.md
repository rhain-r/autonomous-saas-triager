---
description: Run one sandbox ticket end to end and explain what the gates did
---

Triage `$ARGUMENTS` (a ticket id such as `TCK-3021`, or empty for the whole queue).

1. Run `uv run triage run $ARGUMENTS --simulate --verbose`.
2. Read the trace and answer these, concretely:
   - Which tool call produced the evidence the decision rests on?
   - Did anything fail a gate — a fabricated citation, an unlinked chain, a
     patch that would not apply, a challenge that could not complete?
   - If it escalated: is the cited file actually the cause, or merely a file
     that shares vocabulary with the error?
   - If it closed: what would a defect have looked like here, and would the
     challenger have seen it?
3. Compare against the answer key in `agent/evals/golden/$ARGUMENTS.yaml`. If the
   outcome differs, say which stage went wrong — classification, retrieval,
   attribution, or routing. Do not adjust the answer key to match the output.
