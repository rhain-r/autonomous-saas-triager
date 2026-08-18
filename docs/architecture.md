# Architecture

## The problem this shape exists for

An L1 triage agent produces two kinds of output that cost real money: an
**escalation**, which spends engineering time, and a **resolution**, which closes
a customer's ticket. A language model produces the wrong one in exactly the same
confident register as the right one.

The two errors are not symmetric.

| | What it costs | Who finds out |
| --- | --- | --- |
| **Escalating a ghost** | An afternoon of one developer's attention, and trust in the agent | The developer, within a day |
| **Closing a real defect** | The defect persists; the only report of it is destroyed | Nobody |

An escalation is self-correcting. A closure is terminal. Every structural
decision here follows from that asymmetry, and the system spends its verification
budget almost entirely on the irreversible direction.

## Two questions, two mechanisms

The system asks two questions about every ticket, and they are genuinely
different questions, so they get different machinery.

**Is the blamed code actually implicated?** Answered deterministically in
`agent/evidence.py`. No model gets a vote.

**Was "not a defect" the right call?** Answered adversarially in
`agent/challenger.py`, by a second model from a different lab.

Neither substitutes for the other, and it is worth being precise about why:

- The evidence gate **cannot tell a defect from correct behaviour**. In the
  sandbox, `TCK-5210` is an export rejected for exceeding a published 50 MB
  limit. The log line and `src/exports/uploader.ts` link perfectly — because that
  file is the code that correctly emitted the rejection. Attribution is proven;
  defectiveness is not, and no lexical statistic will ever establish it.
- The challenger **cannot tell a real citation from an invented one**. So its own
  findings go back through the same gate the investigator faced.

## The pipeline

```
[ Ticket ]
     │
     ▼
[ Classify ]  Claude → bug | user_error | how_to | feature_request
     │
     ▼
[ Gather ]  deterministic, always runs, no model in the loop
     ├─ search_error_logs(user, ±6h)          the reporter's own failures
     ├─ read_log_window(anchor, ±3)           the INFO lines the error omits
     ├─ search_error_logs(code, all users)    "is it just them?" → blast radius
     ├─ search_codebase(error signature)      what the log said, not what the customer said
     ├─ read_file(top 3 hits)
     ├─ git_log(area)
     └─ search_docs(ticket)
     │
     ▼
[ Hypothesise ]  Claude → statement + event_ids + file_quotes
     │
     ▼
[ Verify ]  agent/evidence.py — deterministic
     ├─ cite_log(event_id)     ─────► unresolved ⇒ recorded as a fabrication
     ├─ cite_code(path, quote) ─────► not in the file ⇒ recorded as a fabrication
     ├─ joint 1: log → code    ─────► error-signature tokens ∩ code tokens
     └─ joint 2: ticket → log  ─────► reporter's user id, or shared vocabulary
     │
     ▼
[ Route ]
     ├─ bug        + linked chain  ⇒ ESCALATE
     ├─ bug        + no proof      ⇒ NEEDS_HUMAN
     ├─ user_error + linked chain  ⇒ RESOLVE  ──┐
     ├─ user_error + no proof      ⇒ NEEDS_HUMAN│
     └─ how_to / feature_request   ⇒ RESOLVE  ──┤
                                                │
     ┌──────────────────────────────────────────┘
     ▼
[ Challenge ]  Gemini, only on auto-closes, burden inverted
     ├─ overturned (with a chain that passes the same gate) ⇒ ESCALATE
     ├─ upheld                                              ⇒ RESOLVE
     └─ needs_human / error / unprovable claim              ⇒ NEEDS_HUMAN
     │
     ▼
[ Patch ]  Claude → PatchProposal(old_text, new_text)
     └─ patcher.verify_patch(): anchor located, unique, non-empty ⇒ diff
     │
     ▼
[ Deliver ]  Jira payload + receipt · customer reply · risk score · trace
```

## Component map

| Module | Responsibility | Calls a model? |
| --- | --- | --- |
| `schemas.py` | Every message contract, and the validators that enforce the rules | no |
| `config.py` | Models, keys, limits, paths | no |
| `llm.py` | `ModelClient` protocol, Anthropic/Gemini adapters, structured output with retry | — |
| `triage_agent.py` | The loop, and the routing table | orchestrates |
| `evidence.py` | Citation checking and the two link joints | no |
| `challenger.py` | The adversarial check on auto-closes | yes |
| `patcher.py` | Anchor location, uniqueness, diff generation | no |
| `reporter.py` | Risk score, tracker payload, markdown, terminal | no |
| `code_tools.py` | `search_codebase`, `read_file`, `cite_code`, `git_log` | no |
| `tools/logs.py` | `search_error_logs`, `read_log_window`, `cite_log` | no |
| `tools/kb.py` | `search_docs` | no |
| `tools/tracker.py` | `FileSink`, `JiraSink` | no |

Roughly three-quarters of the system is deterministic Python. Everything that
*can* be a tested function instead of a prompt, is one — which is why the whole
suite runs in under four seconds with no API key and no cost.

## The evidence chain in detail

A chain is two joints, and both must hold before anything is escalated.

### Joint 1 — log → code

`link_terms()` intersects the tokens of the **error signature** (`code`,
`message`, `detail`) with the tokens of the cited source.

Signature, not the raw line. A raw log line carries a timestamp, a service name,
and a user id; matching on those would link every event to every file in the same
directory.

Tokenisation folds identifier styles together, so a log field `redirect_uri`
matches the symbol `redirectUri`. Without that, the linker would never connect a
log field to the code that produced it — the most common shape real evidence
takes.

`LINK_NOISE` removes words that appear in every web service (`request`,
`response`, `failed`, `handle`, `count`). They are perfectly good *search* terms
and worthless as *proof of a connection*.

### Joint 2 — ticket → log

`anchor_to_ticket()` requires that the cited events either carry the reporting
user's id, or share diagnostic vocabulary with what the customer wrote.

This joint was added after the system shipped without it and produced this: a
customer reported a blank dashboard; the challenger's log query matched nothing;
retrieval fell back to "every failure in the last 24 hours"; and the agent
assembled the previous day's password-reset failures from three unrelated
accounts into a coherent, entirely fictional root cause, escalated at *critical*
severity, citing `src/api/client.ts`. Every citation in it was real. Joint 1
passed. The chain was internally flawless and about somebody else entirely.

Two changes came out of that, and both are load-bearing:

1. **No fallback to "everything recent."** If a ticket's vocabulary matches no
   log events, that is the finding. `agent/challenger.py` no longer widens, and
   when there are no events it does not show source files either — code with no
   failure to attach it to is not evidence, it is raw material for an invention.
2. **Joint 2 itself**, so that the same failure cannot recur through a different
   retrieval path.

### What this is not

Lexical overlap is a floor, not a ceiling. A genuine cause can share no
vocabulary with its symptom, and this gate will not see it. That is why an
unlinked chain routes to `needs_human` and is never discarded: the system's
answer to "I cannot prove this" is a person, not silence.

## Why the challenger only looks at closures

Verification is not free, and spending it symmetrically would be spending it
badly. An escalation already has a human in its future; a closure does not. So
the challenger runs on every proposed auto-close and on nothing else.

The measured cost of that choice is a persistent false-escalation rate — one in
eight tickets in the current eval. See
[Known limitations](#known-limitations).

Three rules keep the challenge honest:

1. **Different inputs.** A window four times wider, unfiltered by user, plus the
   code path the customer described rather than the one the investigator picked.
   Re-reading the same evidence and agreeing is agreement, not verification.
2. **Overturning requires proof.** The challenger's citations go through
   `build_chain()` exactly as the investigator's did. An overturn it cannot quote
   collapses to `needs_human` — which still blocks the close, because an unproven
   suspicion is a reason not to close even when it is not a reason to escalate.
3. **Failure is never agreement.** Timeouts, provider errors, and malformed
   output all return `needs_human`. Silence must not read as consent.

If no challenger is configured, `require_challenge` (default on) downgrades every
auto-close to `needs_human` rather than letting it through unchecked.

## Why models never write diffs

Models hallucinate context lines and mis-count hunk offsets, and a patch that
looks plausible and applies to nothing is worse than no patch: it looks like
work, and the engineer finds out only after trying it.

So `PatchProposal` carries `old_text` and `new_text`, and `patcher.py` does the
rest — locates the anchor in the real file, checks it is unique, performs the
replacement, and generates the diff from the before and after it just computed.
A diff produced that way applies by construction. The remaining checks are the
ones that matter: anchor missing, anchor ambiguous, edit is a no-op.

An anchor that fails is a rejection carried into the report, not an exception. An
engineer wants to know that the model's fix quoted code that is not there.

## Determinism

| Decided by | Things |
| --- | --- |
| **Python** | Which logs to read · which files to open · whether a citation is real · whether evidence links · severity · blast radius · risk score · whether a patch applies · the tracker payload |
| **A model** | Intent · the root-cause hypothesis · the patch content · the customer reply |

Severity is worth calling out. A model asked to rate severity rates the
customer's adjectives. `infer_severity()` reads it off the evidence instead: how
many distinct accounts appear in the cited events, and whether anything actually
errored.

## Known limitations

**The false-escalation rate is real and unaddressed.** `TCK-5210` — a 320 MB
export against a documented 50 MB ceiling — is escalated in every configuration.
The classifier reads "broken", "failing", and "worked fine last quarter" as
defect signals, and the evidence gate cannot contradict it because the chain is
genuine. Only a challenge on the *escalation* direction would catch this.
Symmetric challenge is the top of the backlog; it was deliberately not built
first, because it spends budget on the recoverable error.

**The challenger has a blind spot at one account.** Its heuristic is "the same
failure across unrelated accounts". `TCK-4488` — a duplicate Stripe charge caused
by a missing idempotency check — affects exactly one customer, and is silently
closed under the keyword-only classifier. A defect that hits one account looks
identical to one customer's confusion, and nothing in this architecture currently
distinguishes them.

**Attribution is weaker than detection.** In the current eval the pipeline
escalates the right tickets and names the right file only half the time (2 of 4).
`TCK-9302` is escalated citing `src/api/rate_limit.ts` — which is correct code —
when the cause is the un-delayed retry loop in `src/api/client.ts`. Sending a
developer to the wrong file is a smaller failure than not sending them at all,
but it is still a failure, and the eval reports it as `wrong` rather than
rounding it up.

**The link gate is lexical.** It proves shared vocabulary, not causation, and it
will not see a cause that shares no words with its symptom. Embedding-based
linking would raise the ceiling; it would also make the gate unexplainable, which
is a real cost for something whose whole job is to be checkable.

**Log parsing assumes one format.** `tools/logs.py` parses the key-value shape
that `sandbox/repo/src/platform/logger.ts` emits. A real deployment with three
log formats needs three parsers, or a normalising layer above them.

**Never measured against real models.** Every number in this repository comes
from deterministic lexical stand-ins. The system is wired for Claude and Gemini
and will run against them, but it has not been, so no claim about model accuracy
appears anywhere here. See [`agent/evals/simulated.py`](../agent/evals/simulated.py).

## Extending it beyond the sandbox

The sandbox is four directories of real files, not a mock layer. Replacing it is
a change to `agent/config.py` paths and to the tool implementations behind them —
no agent module moves.

| Sandbox | Production equivalent |
| --- | --- |
| `sandbox/repo/` | A git checkout, or the GitHub contents API |
| `sandbox/logs/` | Datadog, CloudWatch, Loki — `search_error_logs()` becomes a query |
| `sandbox/kb/` | Zendesk Guide, Intercom Articles |
| `sandbox/tickets/` | A Zendesk webhook |
| `FileSink` | `JiraSink`, already written; opt-in via `--live` |

The one thing that must survive the swap is `cite_log()` and `cite_code()`
returning `None` for anything they cannot find. If a production log client
returns a plausible-looking result for an event id that does not exist, the whole
guarantee is gone.
