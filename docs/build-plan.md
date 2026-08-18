# Build plan

A record of how this was built, what changed mid-build and why, and what is left.
Written mainly so the decisions that look arbitrary can be checked against the
reasons that produced them.

Built with [Claude Code](https://claude.com/claude-code).

## Phase 1 — The estate

Before any agent code, a world for it to investigate: `agent/sandbox/` with a
small TypeScript service tree, service logs in the format its own logger emits,
help-centre articles, and an eight-ticket inbox.

Three rules for the sandbox, all of which matter:

- **Real files, not fixtures generated at import.** `cite_code()` is only
  meaningful if it checks actual bytes, and a line number in a report should be
  one you can open.
- **Defects are never labelled.** No `// BUG:` comments. A bug you can grep for
  tests nothing.
- **The logs are what the code would emit.** `src/platform/logger.ts` defines the
  key-value shape, and `tools/logs.py` parses exactly that.

Four genuine defects planted: an OAuth redirect built from the staging origin, a
reset-token TTL comparing milliseconds against seconds, a Stripe webhook with no
idempotency check, and a retry loop with no backoff. Two non-defects that look
like defects: an export over a published limit, and a client-side blank
dashboard.

## Phase 2 — Contracts and gates

`schemas.py` first, then the deterministic tools, then the citation gates. The
validators went in before anything could produce a `Diagnosis`, so there was
never a window in which an unsupported escalation was representable.

`patcher.py` was built around the decision not to ask models for diffs. That was
settled early: the failure mode of a plausible non-applying patch is
indistinguishable from success until an engineer tries it.

## Phase 3 — The loop, and the first honest run

The first end-to-end run on `TCK-3021` worked: `src/auth/config.ts:39`, link
strength 1.00, a correct escalation.

Then the queue ran, and three things were wrong.

### 3a. The system invented a root cause

`TCK-7714` — a blank dashboard with no server-side evidence — came back
**escalated at critical severity, risk 96/100**, citing `src/api/client.ts`, with
evidence drawn from the previous day's password-reset failures across three
unrelated accounts.

The cause was four lines in `challenger.py`: when the ticket's vocabulary matched
no log events, retrieval fell back to *every failure in a 24-hour window*. Every
citation that followed was real. The log↔code joint passed, because the retry
logs genuinely do relate to the retry code. The chain was internally flawless and
about somebody else entirely.

Two changes, both kept:

1. **The fallback is gone.** If nothing matches the ticket, that is the finding.
   And when there are no events, the challenger is no longer shown source files
   either — code with no failure to attach it to is raw material for an
   invention, not evidence.
2. **A second joint on the chain.** `anchor_to_ticket()` requires the cited
   events to carry the reporting user's id or share vocabulary with what the
   customer wrote, so the same failure cannot recur through another retrieval
   path. This is `EvidenceChain.ticket_anchor`, and `is_linked` now requires
   both joints.

The regression is pinned by
`test_a_chain_from_someone_elses_incident_fails_the_ticket_anchor`.

### 3b. One shared word was enough to match

The same investigation surfaced the retrieval bug underneath: a customer writing
*"request a new one"* matched a log line reading *"retrying upstream request"* on
the single token `request`.

`_filter_by_query()` now matches against the event **signature** rather than the
raw line, and requires two shared terms for queries of four or more terms.

### 3c. Closures were being handed out for free

Routing sent every non-`bug` intent straight to `resolve`, which meant
`user_error` with zero evidence auto-closed.

That is backwards. `user_error` is the assertion *"the product worked
correctly"* — a claim about product behaviour, and the claim with no second
chance. It now requires the same linked chain a defect claim does. Only `how_to`
and `feature_request`, which make no claim about behaviour, are answerable from
the knowledge base alone.

This also fixed `TCK-7714` for the right reason rather than by special-casing it.

### 3d. Severity was measuring one customer

The investigator scoped its log search to the reporting user, so `TCK-3021`
reported a blast radius of 1 when four accounts were hitting the same rejection.
`gather()` now runs a second search across all users for the error codes it
already found — *"is it just them?"*, the question a real L1 engineer asks next.
It widens the count, never the hypothesis.

## Phase 4 — Evaluation

The first ablation was misleading. "No challenger" scored zero silent closures —
because with `require_challenge` on and no challenger configured, it refuses to
close anything at all. That is not a baseline, it is a system that does not work.

`require_challenge` became a setting so the eval could measure the honest
comparison: **closing on the classifier's word alone**, which is what an ordinary
LLM triage bot does.

## Where it stands

**Working:** tools, citation gates, both link joints, the loop, routing, the
challenger, the patch gate, tracker sinks, reporting, CLI, eval harness.
132 tests, `ruff` clean, whole suite in about four seconds with no network.

**Measured, in `agent/evals/results/`:** the challenger halves silent closures
against the unguarded baseline (2 → 1 of 8 tickets). Attribution is correct on 2
of 4 scored escalations. One false escalation persists in every configuration.

**Never measured against real models.** Every number comes from deterministic
lexical stand-ins. The system is wired for Claude and Gemini and will run against
them, but it has not been, so no claim about model accuracy appears anywhere in
this repository.

## Backlog, in priority order

1. **Symmetric challenge.** The only thing that catches `TCK-5210`, the false
   escalation present in all four configurations. Deliberately not built first:
   escalation errors are recoverable and closure errors are not, so the budget
   went to the irreversible direction. That reasoning is sound and the cost of it
   is now quantified, which is the point at which it becomes worth paying.
2. **A single-account defect signal.** The challenger's heuristic is "the same
   failure across unrelated accounts", so `TCK-4488` — one customer, one
   duplicate charge — slips through. Ledger self-inconsistency is a defect at any
   blast radius, and severity classes of error code would probably capture it.
3. **Better attribution.** `TCK-9302` is escalated against `rate_limit.ts`, which
   is correct code, rather than `client.ts`, which is not. Ranking candidate
   files by whether they *emit* the cited log line, rather than by shared
   vocabulary, is the obvious next move.
4. **A real run against Claude and Gemini**, with the numbers reported separately
   and labelled as model results, never merged with the simulated ones.
5. **Multi-format log parsing.** One parser today; a real estate has several.
6. **Ticket deduplication.** Four customers reporting one outage should produce
   one escalation with four sources, not four escalations.
