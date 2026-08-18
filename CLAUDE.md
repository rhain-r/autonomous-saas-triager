# CLAUDE.md

Instructions for Claude Code working in this repository. Read before writing code.

## What this system is

An autonomous Level 1 support engineer. It reads a ticket, searches real service
logs and a real source tree, states a root cause, drafts a candidate patch, and
either escalates to a tracker or answers the customer and closes.

The hard problem is **not** classification. It is that a wrong answer arrives in
exactly the same confident register as a right one, and the two ways of being
wrong cost very differently:

- **Escalating a ghost** — a detailed, plausible RCA blaming a file that has
  nothing to do with the symptom. Expensive, and *self-correcting*: a developer
  opens it, finds nothing, and says so.
- **Closing a real defect** — a help article, a polite reply, a shut ticket.
  Cheap-looking, and *terminal*: nobody looks again, and the only report of the
  bug is gone.

Design every decision against the second one.

## Non-negotiable rules

1. **No escalation without a linked chain.** A `Diagnosis` with
   `disposition=escalate` requires an `EvidenceChain` whose citations resolved
   against real files *and* whose two joints hold: the error signature shares
   diagnostic terms with the cited code, and the cited events tie back to this
   ticket. Enforced in `agent/schemas.py` — do not weaken those validators.
2. **A closure is a claim about the product, and needs the same evidence.**
   `intent=user_error` is the assertion "it worked correctly". Without a linked
   chain it routes to `needs_human`, never to `resolve`. Only `how_to` and
   `feature_request` may be answered from the knowledge base alone.
3. **No LLM output is trusted as a dict.** Everything crossing an agent boundary
   goes through a Pydantic model. Validation failure is a retry that feeds the
   error back to the model; it is never a coerce.
4. **The challenger gets different inputs than the investigator.** A wider
   window, unfiltered by user, and the code path the customer described. Asking
   the same question of the same evidence and getting the same answer is
   agreement, not verification.
5. **A challenger failure is never agreement.** Errors, timeouts, and malformed
   output all return `needs_human`.
6. **Never widen retrieval to "everything recent" when a query matches nothing.**
   An empty result is a finding. This rule exists because an earlier version did
   widen, and confidently explained a blank dashboard with the previous day's
   password-reset failures from three unrelated accounts.
7. **Models never emit diffs.** They emit `PatchProposal(old_text, new_text)`;
   `agent/patcher.py` locates the anchor in the real file and generates the diff
   itself. An anchor that is missing or ambiguous is a rejection, not a guess.
8. **Determinism where possible.** Retrieval, citation checking, linking,
   severity, risk scoring, and rendering are plain Python and unit-testable.
   Only reasoning goes to a model.
9. **Never commit customer data.** `*.log` is git-ignored except the synthetic
   fixtures under `agent/sandbox/logs/`.

## Repository shape — keep it this way

Four source subdirectories: `.claude/`, `agent/`, `assets/`, `docs/`. **Do not
add a fifth** without asking — this constraint is deliberate. All new Python goes
under `agent/`, including tests, evals, and the sandbox.

`.github/` is the one exception, and it is platform metadata rather than a source
directory. Root files are `README.md`, `LICENSE`, `CLAUDE.md`, `.gitignore`,
`.gitattributes`, `pyproject.toml`, `.python-version`, `uv.lock`.

**`pyproject.toml` must stay at the repository root.** Python packaging requires
the project root to be the *parent* of the package directory; moving it into
`agent/` breaks `uv run`, the `triage` entry point, and pytest discovery.

```
agent/
  schemas.py        message contracts — the architecture lives here
  config.py         models, keys, limits, paths; nothing hardcoded elsewhere
  llm.py            ModelClient protocol + provider adapters
  triage_agent.py   the loop: classify, gather, hypothesise, verify, route
  evidence.py       citation checking and the two link joints   (no LLM calls)
  challenger.py     the adversarial check on every auto-close
  patcher.py        patch generation gate                        (no LLM calls)
  reporter.py       scoring, tracker payloads, rendering         (no LLM calls)
  code_tools.py     search_codebase, read_file, cite_code, git_log (no LLM calls)
  tools/            logs.py, kb.py, tracker.py                   (no LLM calls)
  prompts/          *.md system prompts, on disk so they show up in diffs
  sandbox/          repo/, logs/, kb/, tickets/ — the simulated estate
  evals/            golden keys, deterministic stand-ins, scoring harness
  tests/            pytest suite
```

## Stack

| Concern | Choice |
| --- | --- |
| Python | 3.12 pinned via `uv` (`requires-python >=3.10,<3.14`) |
| Classification, investigation, patching | Anthropic Claude |
| Challenge | Google Gemini — a different lab, on purpose |
| Validation | Pydantic v2, `extra="forbid"` |
| CLI | Typer + Rich |
| Test / lint | `pytest` (asyncio auto mode), `ruff` |

No agent framework. `agent/llm.py` defines a one-method `ModelClient` protocol;
a LangChain or LangGraph adapter would be a single class. Keep that boundary
clean — it is also what lets the whole suite run offline.

## Commands

```bash
uv sync
uv run pytest
uv run ruff check . --fix
uv run triage tickets                      # no API key needed
uv run triage tools                        # no API key needed
uv run triage inspect TCK-3021             # no API key needed
uv run triage run TCK-3021 --simulate -v   # no API key needed
uv run python -m agent.evals.run           # no API key needed
uv run triage run TCK-3021 --markdown      # needs ANTHROPIC_API_KEY
```

## Conventions

- `from __future__ import annotations` at the top of every module. Type hints on
  every public function.
- System prompts live in `agent/prompts/*.md`, loaded via `config.load_prompt()`.
  They are the real logic of this system and belong in diffs, not string
  literals.
- Model names and numeric limits live only in `agent/config.py`.
- Tests run against `StubClient` or the simulated clients. Never add a test that
  makes a real API call.
- Test the rejection paths, not just the happy path. A gate is only worth having
  if you have confirmed what it refuses.
- Sandbox defects are never labelled with a comment. A bug you can grep for
  tests nothing.
- Commits: Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`).

## Definition of done

- `uv run pytest` passes and covers the failure path.
- `uv run ruff check .` is clean.
- `uv run python -m agent.evals.run` still shows zero silent closures in the
  challenged configurations.
- No new hardcoded model names outside `config.py`.
- No new top-level directory.
- If agent behaviour changed, say so explicitly in the PR body — and if a golden
  ticket now fails, report it rather than editing the key.

## When you are unsure

Ask rather than guess on: whether a given sandbox behaviour is a defect or
working as designed, what counts as sufficient evidence to escalate, and any
change to the `Diagnosis`, `EvidenceChain`, or `ChallengeResult` validators.
Those are policy decisions about what this system is allowed to do to a customer
and to an engineering backlog, not implementation details.
