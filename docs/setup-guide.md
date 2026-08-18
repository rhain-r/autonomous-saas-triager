# Setup guide

## Prerequisites

- [uv](https://docs.astral.sh/uv/). Nothing else — `uv` fetches its own CPython
  3.12 and your system Python is untouched.
- API keys only if you want the agent to run against real models. Four of the
  five commands do not need one.

```bash
git clone https://github.com/rhain-r/autonomous-saas-triager.git
cd autonomous-saas-triager
uv sync
```

## Run it with no API key

Start here. You can watch every gate work before deciding whether to spend a
token on it.

```bash
uv run triage tickets
```

```
                          Inbound queue
┌──────────┬──────────────────┬──────────────────────┬──────────────────────────────┐
│ Ticket   │ Opened           │ Account              │ Subject                      │
├──────────┼──────────────────┼──────────────────────┼──────────────────────────────┤
│ TCK-3021 │ 2026-08-14 09:31 │ Halberd Logistics    │ Cannot log in - page just …  │
│ TCK-1102 │ 2026-08-14 09:40 │ Perrin & Vale        │ How do I change my password? │
│ …        │                  │                      │                              │
└──────────┴──────────────────┴──────────────────────┴──────────────────────────────┘
```

Inspect one ticket with the evidence sitting around it:

```bash
uv run triage inspect TCK-3021
```

Run the full investigation with deterministic stand-ins instead of models:

```bash
uv run triage run TCK-3021 --simulate --verbose
```

Run the whole queue, or the eval:

```bash
uv run triage run --simulate
uv run python -m agent.evals.run
```

See what the agent can do and what it acts on:

```bash
uv run triage tools
```

## Add keys

Create `.env` at the repository root. It is git-ignored.

```dotenv
# Classification, investigation, and patch drafting.
ANTHROPIC_API_KEY=sk-ant-...

# The challenger. A different lab on purpose — see docs/architecture.md.
GOOGLE_API_KEY=...

# Optional overrides; the defaults are in agent/config.py.
# INVESTIGATOR_MODEL=claude-sonnet-5
# CHALLENGER_MODEL=gemini-2.5-pro
# LOG_WINDOW_HOURS=6
# TICKET_CONCURRENCY=4
# JIRA_PROJECT=SUP
```

Then:

```bash
uv run triage run TCK-3021 --verbose --markdown
```

### Running with only an Anthropic key

The system starts, and tells you what it is giving up:

```
No challenger available: GOOGLE_API_KEY is required for the challenger.
Every auto-close will be routed to a human instead of being closed unchallenged.
```

That is the intended behaviour, not a degraded mode with a warning. An
unchallenged auto-close is the one outcome with no safety net, so
`require_challenge` (default on) sends those tickets to a person instead.

To run the challenge on Claude as well — a weaker check, since a shared training
set means a shared blind spot:

```dotenv
CHALLENGER_PROVIDER=anthropic
CHALLENGER_MODEL=claude-sonnet-5
```

## Escalating to a real Jira

Escalations write to `runs/escalations/` by default, as the exact JSON payload a
Jira REST call would carry. To create real issues:

```dotenv
JIRA_API_TOKEN=...
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_PROJECT=SUP
```

```bash
uv run triage run TCK-3021 --live
```

`--live` is the only way to reach a real tracker; nothing selects it implicitly.
Without a token it raises rather than silently dropping the escalation on the
floor. `JiraSink` needs `httpx`, which is already a transitive dependency of the
`anthropic` client.

## Commands

| Command | Needs a key | What it does |
| --- | --- | --- |
| `triage tickets` | no | List the inbound queue |
| `triage inspect TCK-3021` | no | Ticket, its failures, matching help articles |
| `triage tools` | no | The action space and the estate it acts on |
| `triage run TCK-3021 --simulate` | no | Full investigation with stand-ins |
| `triage run TCK-3021` | yes | Full investigation with Claude and Gemini |
| `triage run` | yes | The whole queue, concurrently |
| `triage evaluate` | no | Golden-ticket eval |

Useful flags: `--verbose` (tool trace), `--markdown` (full report),
`--save` (write JSON to `reports/`), `--naive` (simulate with the keyword-only
classifier), `--live` (real tracker).

## Development

```bash
uv run pytest                      # 132 tests, ~4s, no network
uv run ruff check . --fix
uv run python -m agent.evals.run
```

The whole suite runs against `StubClient` and the simulated clients. Never add a
test that makes a real API call.

## Troubleshooting

**`uv sync` fails with a Python minor-version link error on Windows.** Pass the
interpreter explicitly:

```bash
uv sync --python 3.12
```

**`triage run` exits with `ANTHROPIC_API_KEY is required`.** Either add the key
to `.env` or add `--simulate`. There is no partial mode: an investigation with no
investigator would produce a confident empty answer, which is worse than an
error.

**Mojibake in the terminal on Windows** (`—` rendering as `â€"`). The console
code page is not UTF-8:

```bash
chcp 65001
```

**Every ticket comes back `needs_human`.** Expected with no challenger
configured — see *Running with only an Anthropic key* above. To confirm, look for
this in the notes:

```
• no challenger configured; refusing to auto-close unchallenged
```

**An escalation you expected did not happen.** Run with `--verbose` and read the
notes. The three refusals, in the words the system uses:

| Note | What went wrong |
| --- | --- |
| `N claimed citation(s) did not resolve` | The model quoted logs or code that do not exist |
| `…share no diagnostic terms…` | Real citations, unproven connection between them |
| `This is somebody else's incident.` | The cited events do not tie back to this ticket |

**A golden ticket started failing after you changed something.** That is the
eval doing its job. Report it; do not edit the answer key to match the new
output.
