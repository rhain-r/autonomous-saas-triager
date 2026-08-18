"""Turning a model's hypothesis into checked evidence. No LLM calls live here.

A model can say anything about a log line or a file. This module is where that
stops being enough:

1. Every claimed log event is resolved against the real log files.
2. Every claimed code quote is resolved against the real source file.
3. **log → code:** the tokens of the error signature are intersected with the
   tokens of the cited source.
4. **ticket → log:** the cited events must carry the reporting user's id, or
   share vocabulary with what the customer wrote.

Steps 1 and 2 prove the agent quoted real things. Steps 3 and 4 ask whether
those real things have anything to do with each other, and with the customer.
Both are easy to omit and expensive to skip:

- Without step 3, an agent that greps "auth", lands on `src/auth/session.ts`,
  and quotes it accurately passes every check while blaming a file that shares
  not one term with the error it claims to explain.
- Without step 4, a chain can be flawless in isolation and still be somebody
  else's incident. This is not hypothetical — an earlier version of this system
  explained a blank dashboard with the previous day's password-reset failures
  from three unrelated accounts, and escalated it as critical.

What this is not: proof of causation. Lexical overlap is a floor, not a ceiling
— a genuine cause can share no vocabulary with its symptom. Chains that fail to
link are therefore routed to a human, never discarded.
"""

from __future__ import annotations

from agent.code_tools import cite_code
from agent.schemas import Commit, EvidenceChain, Hypothesis, LogEvent, Ticket, tokenize
from agent.tools.logs import cite_log

# Terms that link almost anything to almost anything in a web service: they
# appear in every stack and carry no diagnostic information. Kept separate from
# the global STOPWORDS because these are specific to *this* comparison — they
# are perfectly good search terms, just worthless as proof of a connection.
LINK_NOISE: frozenset[str] = frozenset(
    {
        "request", "response", "server", "client", "service", "api", "call",
        "failed", "failure", "reject", "rejected", "invalid", "missing",
        "start", "started", "complete", "done", "run", "job", "event", "events",
        "get", "post", "put", "set", "add", "check", "handle", "process",
        "time", "timestamp", "date", "count", "total", "size", "limit",
    }
)


def link_terms(events: list[LogEvent], snippets: list[str]) -> list[str]:
    """Terms shared between what the logs say went wrong and what the code says.

    Compares the *error signature* — code, message, detail — against the cited
    source, not the whole log line. A log line carries its service name, its
    user id, and its timestamp; matching on those would link every event to
    every file in the same directory.
    """
    log_tokens: set[str] = set()
    for event in events:
        log_tokens |= tokenize(event.signature)

    code_tokens: set[str] = set()
    for snippet in snippets:
        code_tokens |= tokenize(snippet)

    shared = (log_tokens & code_tokens) - LINK_NOISE
    return sorted(shared)


def anchor_to_ticket(ticket: Ticket, events: list[LogEvent]) -> str:
    """Why these log events belong to *this* complaint, or "" if they do not.

    The second joint of the chain, and the one that is easy to forget. Log
    events and code can be perfectly consistent with each other while having
    nothing to do with the customer who wrote in — a blank dashboard explained
    with yesterday's rate-limit errors from a different account is internally
    coherent and completely wrong.

    Two things count as an anchor, in order of strength: the events carry the
    reporting user's id, or they share diagnostic vocabulary with what the
    customer actually wrote.
    """
    if ticket.user_id and any(e.user_id == ticket.user_id for e in events):
        return f"user {ticket.user_id} appears in the cited events"

    ticket_tokens = tokenize(f"{ticket.subject} {ticket.body}")
    shared: set[str] = set()
    for event in events:
        shared |= tokenize(event.signature) & ticket_tokens
    shared -= LINK_NOISE

    if shared:
        return f"shared vocabulary with the ticket: {', '.join(sorted(shared))}"
    return ""


def build_chain(
    ticket: Ticket,
    hypothesis: Hypothesis,
    *,
    commits: list[Commit] | None = None,
) -> tuple[EvidenceChain | None, list[str]]:
    """Resolve a hypothesis into a verified chain.

    Returns the chain (or `None` if nothing survived) alongside the list of
    fabrications found. Fabrications are kept and reported rather than quietly
    dropped: a run in which the model invented three of four citations is a
    materially different run from one where it invented none, even when both
    produce the same final answer.
    """
    fabrications: list[str] = []

    events: list[LogEvent] = []
    seen_events: set[str] = set()
    for event_id in hypothesis.event_ids:
        event = cite_log(event_id)
        if event is None:
            fabrications.append(f"log event {event_id!r} does not exist in any log file")
            continue
        if event.event_id not in seen_events:
            seen_events.add(event.event_id)
            events.append(event)

    spans = []
    seen_spans: set[str] = set()
    for claim in hypothesis.file_quotes:
        span = cite_code(claim.path, claim.quote)
        if span is None:
            fabrications.append(
                f"quote attributed to {claim.path} is not in that file: {_clip(claim.quote)}"
            )
            continue
        if span.locator not in seen_spans:
            seen_spans.add(span.locator)
            spans.append(span)

    if not events or not spans:
        return None, fabrications

    return (
        EvidenceChain(
            symptom=ticket.subject,
            log_events=events,
            code_spans=spans,
            link_terms=link_terms(events, [s.snippet for s in spans]),
            ticket_anchor=anchor_to_ticket(ticket, events),
            commits=commits or [],
            reasoning=hypothesis.reasoning,
        ),
        fabrications,
    )


def explain_gap(chain: EvidenceChain | None, fabrications: list[str]) -> str:
    """A sentence a human can act on, for anything that failed to link."""
    if chain is None:
        if fabrications:
            return (
                f"No verifiable evidence survived: {len(fabrications)} claimed citation(s) "
                "could not be found in the logs or the source tree."
            )
        return "No log events and no code spans could be tied to this ticket."
    if not chain.ticket_anchor:
        return (
            "The cited log events cannot be tied back to this ticket: they carry "
            "neither the reporting user nor any vocabulary from what the customer "
            "wrote. This is somebody else's incident."
        )
    if not chain.link_terms:
        files = ", ".join(sorted({s.path for s in chain.code_spans}))
        return (
            f"Cited logs and cited code ({files}) share no diagnostic terms, so the "
            "connection between the symptom and the blamed code is unproven."
        )
    return ""


def _clip(text: str, width: int = 80) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else f"{flat[:width]}..."
