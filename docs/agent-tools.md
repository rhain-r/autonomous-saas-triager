# Agent tools

Six tools, plus three gates. Not one of them calls a language model — every
module in `agent/tools/` and `agent/code_tools.py` is plain, testable Python.

That split is the point. The agent's *reasoning* is a model; the agent's
*perception* and *verification* are code you can unit-test. A tool that
occasionally invents a search result would make every gate above it decorative.

```
uv run triage tools
```

---

## Observation

### `search_error_logs(...)`

`agent/tools/logs.py`

The primary observation tool. Slices parsed log events by user, time window,
level, and text.

| Parameter | Default | Notes |
| --- | --- | --- |
| `user_id` | `None` | The reporting customer. Omit to search the whole estate. |
| `around` | `None` | Usually the ticket's `created_at`. |
| `window_hours` | `6` | `settings.log_window_hours`. |
| `level` | `None` | Exact level. Overrides `failures_only`. |
| `query` | `""` | Matched against the event **signature**, not the raw line. |
| `failures_only` | `True` | WARN and ERROR only. |
| `limit` | `40` | `settings.max_log_events`. |

**Defaults to failures.** An investigator handed every INFO line spends its
context on healthy traffic while the signal it needs is three lines long.

**The query threshold.** A query of four or more terms requires **two** shared
terms with an event's signature; shorter, deliberate queries (an error code, a
symbol name) still match on one.

This exists because of a specific failure. A customer wrote *"I had to request a
new one three times"*; a log line in an unrelated service read *"retrying
upstream request"*; they shared the single word `request`; and that was enough to
pull a different service's incident into the evidence for a password-reset
ticket. One incidental word is not a match.

```python
search_error_logs(user_id="usr_8812", around=ticket.created_at, window_hours=6)
search_error_logs(query="OAUTH_REDIRECT_MISMATCH", limit=40)   # blast radius
```

### `read_log_window(source_file, line_no, before=3, after=3)`

Neighbouring lines around one event, **including INFO**.

What happened just before an error is usually the half of the story the error
line omits: the authorize call that preceded the rejected exchange, the first
delivery of a webhook that then arrived twice. `TCK-4488` is unreadable without
it — the ERROR line says only that the ledger disagrees with itself, and the two
INFO lines above it show the same Stripe event id processed thirty seconds apart.

### `search_codebase(query, *, path_prefix="", max_results=25)`

`agent/code_tools.py`

Ranks source lines by how many distinct query terms they carry, with a bonus when
the file path itself matches.

The agent calls this with the **error signature**, not the customer's words. A
customer says "the login page keeps refreshing"; the log says
`OAUTH_REDIRECT_MISMATCH`; only the second one leads anywhere. Searching the tree
for the customer's vocabulary returns whatever file happens to share it.

### `read_file(path, start=1, end=None)`

Returns a **line-numbered** slice. The numbers are part of the payload on
purpose: they are what the model quotes back, and what `cite_code()` then checks
independently.

### `git_log(path_prefix="", limit=10)`

Recent commits touching a prefix, newest first. Reads
`agent/sandbox/repo/commits.json`; a real deployment shells out to `git log` or
calls the GitHub API and returns the same shape.

Corroboration only. A commit touching the blamed file shortly before the first
occurrence in the logs is a strong signal; a commit touching the same *directory*
is not, and the investigator prompt says so explicitly.

### `search_docs(query, *, limit=3)`

`agent/tools/kb.py`

Ranks help-centre articles, weighting title matches 3× and tag matches 2× over
body mentions. Support articles are titled for the symptom the customer types, so
a title hit is a much stronger signal than the same word appearing once in a
troubleshooting table.

**This is the agent's most dangerous tool.** A plausible article makes closing a
ticket feel finished. `TCK-6033` is a real defect whose wording matches KB-0012
almost exactly — and KB-0012 is not the answer, it is the *specification the
product is violating*. That is precisely why every resolution leaning on an
article gets challenged.

---

## Gates

The tools let the agent see. The gates decide what it is allowed to claim.

### `cite_log(event_id, quote="")` → `LogEvent | None`

Resolves an event id against the real log files, optionally checking that
`quote` really appears in that line. An id the model never saw returns `None`,
and the caller records a fabrication.

### `cite_code(path, quote)` → `CodeSpan | None`

Verifies that `quote` appears in `path` and returns where, with real line numbers
and a SHA of the file contents at read time.

Whitespace is normalised before comparison. Models reflow indentation constantly,
and rejecting a real quote over two spaces would push the agent toward citing
nothing at all — which is the failure this gate exists to prevent, arrived at
from the other direction.

```python
cite_code("src/auth/config.ts", "redirectUri: `${STAGING_ORIGIN}/auth/callback`,")
# → CodeSpan(path=..., line_start=39, line_end=39, content_sha="...")

cite_code("src/auth/config.ts", "const sessionCache = new LruCache(512);")
# → None    plausible, and not in the file
```

### `verify_patch(proposal)` → `Patch`

`agent/patcher.py`

Locates `old_text` in the real file, checks it is unique, applies the
replacement, and generates the diff itself. Rejections, all carried into the
report rather than raised:

| Reason | Why it is refused |
| --- | --- |
| anchor not found | The model quoted code that is not there |
| anchor ambiguous | `return null;` appears twice; editing the wrong one silently is worse than editing neither |
| no-op | `new_text` matches what is already there |
| unreadable path | Including anything outside the sandbox |

---

## Containment

Every path goes through `code_tools._resolve()`, which refuses to leave
`agent/sandbox/repo/`. An investigator that can be talked into reading
`../../.env` is not a support agent, it is an exfiltration path.

Rooted paths (`/etc/passwd`) are re-rooted rather than rejected — the same
contract a chroot gives. Traversal (`../../`) raises `SandboxEscape`.

The tracker is the one tool that reaches outside the process, so it goes through
a named seam. `FileSink` is the default and writes the exact JSON a Jira REST
call would carry. `JiraSink` is real and is never selected implicitly —
`--live` is the only way to reach it, and it refuses to start without a token
rather than silently dropping the escalation.

---

## Adding a tool

1. Put it in `agent/tools/` or `agent/code_tools.py`. **No LLM calls.**
2. Return a Pydantic model from `agent/schemas.py`, not a dict.
3. Add a `ToolName` member so calls appear in the run trace.
4. Call it from `gather()` in `agent/triage_agent.py` and record the step.
5. If it can produce something the agent might cite, write the `cite_*` gate for
   it in the same commit. A tool whose output cannot be verified cannot support
   an escalation, and adding one without its gate quietly widens what the system
   is willing to claim.
6. Test the rejection path.
