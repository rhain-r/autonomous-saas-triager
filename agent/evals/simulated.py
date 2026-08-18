"""Deterministic stand-ins for the four model roles.

These implement `ModelClient` with lexical rules instead of a language model, so
the whole pipeline — tools, evidence gate, challenger, patch gate, tracker,
reporting — runs with no API key, no network, and no cost. That is what makes
the eval free to run in CI and what makes the test suite finish in a second.

**Read this before quoting any number produced with these.** They are not models
and they are not proxies for models. What they measure is *pipeline behaviour*:
whether a bad citation is caught, whether an unlinked chain is blocked, whether
an auto-close is challenged, whether a patch that does not apply is rejected.
All of that is real code with real consequences. Model accuracy is not measured
here at all, and no claim about Claude or Gemini appears anywhere in the eval
output.

The stand-ins never read an answer key. They see exactly what a model sees: the
system prompt and the rendered prompt.
"""

from __future__ import annotations

import json
import re

from agent.schemas import Intent, Verdict, tokenize

# --- Prompt parsing ----------------------------------------------------------

_EVENT_LINE = re.compile(r"^\[([^\]]+)\]\s+(.*)$")
_FILE_HEADER = re.compile(r"^## File: (.+)$")
_NUMBERED = re.compile(r"^\s*(\d+)\s*\|\s?(.*)$")
_TICKET_HEAD = re.compile(r"^Ticket (\S+) — (.*)$", re.M)


def _parse_events(prompt: str) -> list[tuple[str, str]]:
    """Every `[event_id] raw line` the prompt showed."""
    found: list[tuple[str, str]] = []
    for line in prompt.splitlines():
        match = _EVENT_LINE.match(line.strip())
        if match and not match.group(1).startswith("KB-"):
            found.append((match.group(1), match.group(2)))
    return found


def _parse_files(prompt: str) -> dict[str, list[str]]:
    """Every `## File: path` block, as a list of unnumbered source lines."""
    files: dict[str, list[str]] = {}
    current: str | None = None
    for line in prompt.splitlines():
        header = _FILE_HEADER.match(line.strip())
        if header:
            current = header.group(1).strip()
            files[current] = []
            continue
        if current is None:
            continue
        numbered = _NUMBERED.match(line)
        if numbered:
            files[current].append(numbered.group(2))
    return {path: lines for path, lines in files.items() if lines}


def _ticket_text(prompt: str) -> str:
    """The ticket body, which is everything before the first `##` section."""
    head, _, _ = prompt.partition("\n## ")
    return head


def _is_failure(raw: str) -> bool:
    return " ERROR " in raw or " WARN " in raw


def _signature(raw: str) -> str:
    """The diagnostic part of a raw log line: its msg, code, and detail."""
    fields = dict(re.findall(r'(\w+)="([^"]*)"', raw))
    return " ".join(filter(None, (fields.get("code"), fields.get("msg"), fields.get("detail"))))


# --- Shared lexical investigation --------------------------------------------


def _rank_lines(
    files: dict[str, list[str]],
    failure_tokens: set[str],
    all_tokens: set[str],
) -> tuple[str, str, float] | None:
    """Best (path, line, score) across every file shown.

    Failure vocabulary is weighted double. A line that echoes the text of the
    error is far better evidence than one that merely shares words with a
    healthy INFO line in the same window.
    """
    best: tuple[str, str, float] | None = None
    for path, lines in files.items():
        for line in lines:
            if not line.strip() or line.strip().startswith(("*", "//", "/**")):
                continue
            tokens = tokenize(line)
            score = 2.0 * len(tokens & failure_tokens) + 1.0 * len(tokens & all_tokens)
            if score <= 0:
                continue
            if best is None or score > best[2]:
                best = (path, line.strip(), score)
    return best


def _investigate(prompt: str) -> dict[str, object]:
    """Lexical root-cause attribution over whatever the prompt showed."""
    events = _parse_events(prompt)
    files = _parse_files(prompt)

    if not events or not files:
        return {
            "statement": (
                "No server-side evidence connects this ticket to a code path. "
                "Either the failure is client-side or it was not logged."
            ),
            "event_ids": [],
            "file_quotes": [],
            "reasoning": (
                f"{len(events)} log event(s) and {len(files)} file(s) were available; "
                "an attribution needs both."
            ),
        }

    failures = [(eid, raw) for eid, raw in events if _is_failure(raw)]
    failure_tokens: set[str] = set()
    for _, raw in failures:
        failure_tokens |= tokenize(_signature(raw))
    all_tokens: set[str] = set()
    for _, raw in events:
        all_tokens |= tokenize(_signature(raw))

    best = _rank_lines(files, failure_tokens, all_tokens)
    if best is None:
        return {
            "statement": "No line in the files examined shares vocabulary with the errors.",
            "event_ids": [eid for eid, _ in failures],
            "file_quotes": [],
            "reasoning": "Attribution abandoned rather than guessed.",
        }

    path, line, score = best
    quote_tokens = tokenize(line)

    # Cite the failures, plus any surrounding event this quote actually explains.
    cited = [eid for eid, _ in failures]
    cited += [
        eid
        for eid, raw in events
        if eid not in cited and tokenize(_signature(raw)) & quote_tokens
    ]

    return {
        "statement": f"{path} emits or configures the behaviour behind this failure.",
        "event_ids": cited,
        "file_quotes": [{"path": path, "quote": line}],
        "reasoning": (
            f"The error signature and {path} share {len(quote_tokens & failure_tokens)} "
            f"diagnostic term(s) (score {score:.1f}). Disproved by finding the same "
            f"signature emitted from a different file."
        ),
    }


# --- Classifiers -------------------------------------------------------------

_FEATURE = ("add a", "add dark", "roadmap", "feature", "would be great if", "please add")
_HOW_TO = ("how do i", "how can i", "where is", "where do i", "point me", "could not find")

# Phrases in which a customer is quoting the product's own promise back at it.
# These are what separates "I don't understand the product" from "the product is
# not doing what it says", and a classifier that ignores them closes real bugs.
_SPEC_VIOLATION = (
    "says it has expired",
    "immediately",
    "worked fine",
    "have not changed",
    "no error message",
    "keeps refreshing",
    "twice",
    "same thing",
)


def _classify(prompt: str, *, signal_aware: bool) -> dict[str, object]:
    text = _ticket_text(prompt).lower()

    if any(p in text for p in _FEATURE):
        intent, confidence, why = Intent.FEATURE_REQUEST, 0.9, "asks for absent behaviour"
    elif any(p in text for p in _HOW_TO):
        intent, confidence, why = Intent.HOW_TO, 0.8, "asks where an existing capability lives"
    elif signal_aware and any(p in text for p in _SPEC_VIOLATION):
        intent, confidence, why = Intent.BUG, 0.75, "reports documented behaviour failing"
    elif any(w in text for w in ("broken", "failing", "fails", "error", "cannot", "crash")):
        intent, confidence, why = Intent.BUG, 0.6, "reports a failure"
    else:
        intent, confidence, why = Intent.USER_ERROR, 0.5, "no failure signal in the text"

    match = _TICKET_HEAD.search(prompt)
    subject = match.group(2) if match else ""
    # Drop anything carrying a digit. Ticket ids, timestamps, and account
    # numbers tokenise into terms that match nothing and dilute the ones that do.
    terms = sorted(t for t in tokenize(f"{subject} {text}") if not any(c.isdigit() for c in t))[:8]

    return {
        "intent": intent.value,
        "confidence": confidence,
        "rationale": f"Lexical stand-in: {why}.",
        "suspected_area": _area(text),
        "search_terms": terms,
    }


def _area(text: str) -> str | None:
    for area, words in {
        "auth": ("log in", "login", "password", "sso", "okta", "reset", "session"),
        "billing": ("billed", "charge", "invoice", "refund", "payment"),
        "api": ("api", "429", "rate limit", "endpoint"),
        "exports": ("export", "csv", "download", "parquet"),
        "dashboard": ("dashboard", "blank", "widget"),
    }.items():
        if any(w in text for w in words):
            return area
    return None


# --- Clients -----------------------------------------------------------------


class SimulatedClassifier:
    """Keyword intake. `signal_aware=False` is the naive baseline."""

    def __init__(self, *, signal_aware: bool = True) -> None:
        self.signal_aware = signal_aware
        self.name = f"sim-classifier{'' if signal_aware else '-naive'}"

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> str:
        return json.dumps(_classify(prompt, signal_aware=self.signal_aware))


class SimulatedInvestigator:
    """Lexical attribution, and the customer reply template.

    Both live on one client because the real pipeline uses one Claude client for
    the investigation and the reply, and the simulation should exercise the same
    call sites.
    """

    name = "sim-investigator"

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> str:
        if "reply that goes to the customer" in system:
            return _reply(prompt)
        return json.dumps(_investigate(prompt))


class SimulatedPatcher:
    """Marks the cited line for a developer. It does not attempt a real fix.

    A lexical stand-in cannot write a correct code change, and pretending
    otherwise would put a fabricated fix in front of an engineer. What it does
    instead is exercise the patch gate for real: the anchor is located in the
    file, checked for uniqueness, and the diff is generated from the actual
    before and after. With an API key the same gate receives Claude's fix.
    """

    name = "sim-patcher"

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> str:
        files = _parse_files(prompt)
        anchor = _cited_line(prompt)
        if not files or anchor is None:
            return json.dumps(
                {
                    "path": next(iter(files), "src/unknown.ts"),
                    "old_text": "",
                    "new_text": "",
                    "rationale": "no anchor line available",
                    "test_hint": "",
                }
            )
        path, line = anchor
        indent = line[: len(line) - len(line.lstrip())]
        return json.dumps(
            {
                "path": path,
                "old_text": line,
                "new_text": f"{indent}// TODO(triage): verified as the cited cause\n{line}",
                "rationale": (
                    "Simulated patcher: marks the verified line for review rather than "
                    "guessing at a fix. Exercises the patch gate, not model patch quality."
                ),
                "test_hint": "A regression test asserting the documented behaviour at this line.",
            }
        )


class SimulatedChallenger:
    """Tries to prove an auto-close wrong, using breadth the first agent lacked.

    The rule is deliberately simple and deliberately not a keyword list of known
    bugs: overturn when failures matching the ticket's own vocabulary appear for
    **more than one account**, and a file can be quoted that shares diagnostic
    terms with them. One confused customer is a support question; the same
    signature across several unrelated accounts is a defect.
    """

    name = "sim-challenger"

    def __init__(self, *, min_accounts: int = 2) -> None:
        self.min_accounts = min_accounts

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> str:
        events = [(eid, raw) for eid, raw in _parse_events(prompt) if _is_failure(raw)]
        accounts = {
            m.group(1)
            for _, raw in events
            for m in [re.search(r'user="([^"]+)"', raw)]
            if m
        }

        if len(accounts) < self.min_accounts:
            return json.dumps(
                {
                    "verdict": Verdict.UPHELD.value,
                    "reasoning": (
                        f"{len(events)} matching failure(s) across {len(accounts)} account(s) — "
                        "below the threshold at which this stops looking like one customer's "
                        "situation and starts looking like a defect."
                    ),
                }
            )

        found = _investigate(prompt)
        if not found["file_quotes"]:
            return json.dumps(
                {
                    "verdict": Verdict.NEEDS_HUMAN.value,
                    "reasoning": (
                        f"The same failure appears for {len(accounts)} accounts, which should "
                        "not happen if this is user error — but no code path could be quoted "
                        "to prove it."
                    ),
                }
            )

        quote = found["file_quotes"][0]  # type: ignore[index]
        return json.dumps(
            {
                "verdict": Verdict.OVERTURNED.value,
                "reasoning": (
                    f"The same failure signature appears for {len(accounts)} unrelated "
                    f"accounts and is emitted by {quote['path']}. That is a defect, not a "
                    "customer misunderstanding, and closing it would have lost it."
                ),
                "statement": found["statement"],
                "event_ids": found["event_ids"],
                "file_quotes": found["file_quotes"],
            }
        )


def _cited_line(prompt: str) -> tuple[str, str] | None:
    """Recover the quoted line from the evidence block in the patcher prompt."""
    files = _parse_files(prompt)
    for line in prompt.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "`", "symptom:", "logs:", "code:", "[")):
            continue
        for path, lines in files.items():
            for candidate in lines:
                if candidate.strip() and candidate.strip() == stripped:
                    return path, candidate
    return None


# --- Reply -------------------------------------------------------------------

_REPLIES = {
    "escalate": (
        "Thanks for the detail in your report — it was enough to reproduce what you "
        "described.\n\nThis is a defect on our side rather than anything to do with your "
        "configuration. It's now with our engineering team under {ref}, and I'll come "
        "back to you here as soon as there's a fix to confirm."
    ),
    "resolve": (
        "Happy to help with this.\n\n{answer}\n\nIf that doesn't match what you're "
        "seeing, reply here and I'll pick it straight back up."
    ),
    "needs_human": (
        "Thanks for flagging this — I've had a look and I want a specialist on our side "
        "to take it from here rather than guess.\n\nOne thing that would speed this up: "
        "if you can send a screenshot of your browser console (F12, then Console) at "
        "the moment it happens, plus the exact time, that will tell us whether this is "
        "happening in the browser or on our end."
    ),
}


def _reply(prompt: str) -> str:
    decision = "needs_human"
    for line in prompt.splitlines():
        if line.startswith("Decision: "):
            decision = line.split(": ", 1)[1].strip()
    reference = next(
        (line.split(": ", 1)[1].strip() for line in prompt.splitlines()
         if line.startswith("Reference to quote: ")),
        "your ticket",
    )
    answer = next(
        (line.split(": ", 1)[1].strip() for line in prompt.splitlines() if line.startswith("- ")),
        "The steps are in the linked help-centre article.",
    )
    template = _REPLIES.get(decision, _REPLIES["needs_human"])
    return template.format(ref=reference, answer=answer)


def simulated_clients(*, signal_aware: bool = True, with_challenger: bool = True):
    """Assemble a full `AgentClients` bundle of stand-ins."""
    from agent.triage_agent import AgentClients

    return AgentClients(
        classifier=SimulatedClassifier(signal_aware=signal_aware),
        investigator=SimulatedInvestigator(),
        patcher=SimulatedPatcher(),
        challenger=SimulatedChallenger() if with_challenger else None,
    )
