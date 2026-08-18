---
description: Add a sandbox ticket with a matching answer key
---

Add a new ticket to the sandbox for: $ARGUMENTS

1. Write `agent/sandbox/tickets/TCK-####.json` in the voice of a real customer —
   partial information, the wrong vocabulary, and an emotional register that does
   *not* correlate with whether it is a defect.
2. If it is a defect, plant the cause in `agent/sandbox/repo/src/` as code that
   looks reasonable. Never label it with a comment; a bug the reader can grep for
   is not a test of anything.
3. Add the log lines it would produce to the right `agent/sandbox/logs/*.log`
   file, in the format `src/platform/logger.ts` emits. Include the healthy lines
   around them.
4. Write `agent/evals/golden/TCK-####.yaml` with the intent, disposition, the
   root-cause paths, and — if this ticket is a trap — which trap and why.
5. Run `uv run python -m agent.evals.run` and report what the new ticket did.
   If the pipeline gets it wrong, say so and leave it wrong; a failing golden
   ticket is information, and quietly weakening the key to make it pass destroys
   the only thing the eval is for.
