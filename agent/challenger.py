"""The adversarial check on auto-closing a ticket.

An escalation is self-correcting: a developer opens it, finds nothing, and says
so. A resolution is not. When this agent answers a ticket with a help article and
closes it, that is the last time anybody looks — so a real defect closed as
"working as intended" removes the only evidence it existed.

This module exists to make that outcome expensive to reach. Every proposed
auto-close is handed to a **different model from a different lab** with the
burden inverted: not "is this resolution reasonable" but "prove a defect is
there anyway".

Three rules keep it honest:

1. **It gets different inputs.** A wider log window, unfiltered by user, plus
   the source of the code path the customer described. Re-reading the same
   evidence and agreeing is agreement, not verification.
2. **Overturning requires proof.** The challenger's citations go back through
   `agent.evidence.build_chain()`, the same gate the investigator faced. An
   overturn it cannot quote collapses to `needs_human`.
3. **A challenger failure is never agreement.** Timeouts, malformed output, and
   provider errors all return `needs_human`. Silence must not read as consent.
"""

from __future__ import annotations

import logging

from pydantic import Field

from agent import code_tools
from agent.config import Settings, load_prompt
from agent.evidence import build_chain
from agent.llm import ModelClient, complete_structured
from agent.schemas import (
    ChallengeResult,
    Diagnosis,
    Hypothesis,
    StrictModel,
    Ticket,
    Verdict,
    tokenize,
)
from agent.tools import logs

logger = logging.getLogger(__name__)

# How much wider than the investigator's window the challenger looks. The most
# common shape of a missed defect is a pattern that is invisible inside one
# user's slice and obvious across the estate.
WINDOW_MULTIPLIER = 4


class _ChallengeReply(StrictModel):
    """What the challenger model returns, before any of it is checked."""

    verdict: Verdict
    reasoning: str
    statement: str = ""
    event_ids: list[str] = Field(default_factory=list)
    file_quotes: list[dict[str, str]] = Field(default_factory=list)


def _widened_evidence(ticket: Ticket, diagnosis: Diagnosis, settings: Settings) -> str:
    """The evidence packet, deliberately different from the investigator's."""
    window = settings.log_window_hours * WINDOW_MULTIPLIER

    # Unfiltered by user on purpose: one confused customer is a support
    # question, the same signature across four accounts is a defect.
    events = logs.search_error_logs(
        around=ticket.created_at,
        window_hours=window,
        query=f"{ticket.subject} {ticket.body}",
        limit=settings.max_log_events,
    )
    # Deliberately no fallback to "every failure in the window". An earlier
    # version widened to the whole estate when the ticket's vocabulary matched
    # nothing, and handed the challenger unrelated errors from a different day
    # and different users — which it duly assembled into a confident, entirely
    # fictional root cause. If nothing matches the ticket, that is the finding.

    parts: list[str] = []
    if events:
        rendered = "\n".join(f"[{e.event_id}] {e.raw}" for e in events)
        parts.append(f"## Log events across all users (±{window}h)\n\n{rendered}")
    else:
        parts.append(f"## Log events across all users (±{window}h)\n\nNone recorded.")

    # Read the code path the customer described, not the one the investigator
    # chose. If it settled on a help article, it may never have opened a file.
    #
    # Only when there are log events to explain, though. Source shown with no
    # failure to attach it to is not evidence, it is raw material for a
    # confident invention — and a model asked to find a defect will find one.
    if events:
        query = f"{ticket.subject} {ticket.body} " + " ".join(
            dict.fromkeys(e.code or "" for e in events if e.code)
        )
        matches = code_tools.search_codebase(query, max_results=settings.max_code_matches)
        seen: list[str] = []
        for match in matches:
            if match.path not in seen:
                seen.append(match.path)
            if len(seen) >= 2:
                break
        for path in seen:
            contents = code_tools.read_file(path, 1, settings.max_file_lines)
            parts.append(f"## File: {path}\n\n```\n{contents}\n```")
    else:
        parts.append(
            "## Source\n\nNot shown. Nothing in the logs matches this ticket, so there "
            "is no failure for a code path to explain. If you believe a defect is "
            "present, say what evidence you would need — do not reason from source alone."
        )

    if diagnosis.kb_hits:
        articles = "\n".join(f"- {h.article_id} {h.title}: {h.excerpt}" for h in diagnosis.kb_hits)
        parts.append(
            "## Help-centre articles the first agent relied on\n\n"
            f"{articles}\n\n"
            "Treat these as the specification the product is supposed to meet, not "
            "as proof that it does."
        )

    return "\n\n".join(parts)


async def challenge_resolution(
    client: ModelClient,
    ticket: Ticket,
    diagnosis: Diagnosis,
    settings: Settings,
) -> ChallengeResult:
    """Try to prove a proposed auto-close wrong. Never raises."""
    evidence = _widened_evidence(ticket, diagnosis, settings)
    prompt = (
        f"{ticket.render()}\n\n"
        f"## The resolution you must try to disprove\n\n"
        f"Intent: {diagnosis.intent.value}\n"
        f"Summary: {diagnosis.summary}\n"
        f"Confidence: {diagnosis.confidence:.2f}\n\n"
        f"{evidence}"
    )

    try:
        reply = await complete_structured(
            client,
            load_prompt("challenger"),
            prompt,
            _ChallengeReply,
            max_tokens=settings.max_output_tokens,
        )
    except Exception as exc:
        logger.warning("challenger failed on %s: %s", ticket.ticket_id, exc)
        return ChallengeResult(
            ticket_id=ticket.ticket_id,
            verdict=Verdict.NEEDS_HUMAN,
            reasoning=(
                f"The challenger did not complete ({type(exc).__name__}). A failed "
                "check is not agreement, so this ticket is not being auto-closed."
            ),
            challenger_model=client.name,
        )

    if reply.verdict is not Verdict.OVERTURNED:
        return ChallengeResult(
            ticket_id=ticket.ticket_id,
            verdict=reply.verdict,
            reasoning=reply.reasoning,
            challenger_model=client.name,
        )

    # An overturn goes through exactly the gate the investigator faced.
    hypothesis = Hypothesis(
        statement=reply.statement or reply.reasoning,
        event_ids=reply.event_ids,
        file_quotes=[
            {"path": q.get("path", ""), "quote": q.get("quote", "")}
            for q in reply.file_quotes
            if q.get("path") and q.get("quote")
        ],
        reasoning=reply.reasoning,
    )
    chain, fabrications = build_chain(ticket, hypothesis)

    if chain is None or not chain.is_linked:
        detail = (
            f" {len(fabrications)} citation(s) did not resolve." if fabrications else ""
        )
        return ChallengeResult(
            ticket_id=ticket.ticket_id,
            verdict=Verdict.NEEDS_HUMAN,
            reasoning=(
                f"The challenger asserted a defect but could not prove it.{detail} "
                f"Its claim was: {reply.reasoning} — routed to a human rather than "
                "auto-closed, because an unproven suspicion is still a reason not "
                "to close."
            ),
            challenger_model=client.name,
        )

    return ChallengeResult(
        ticket_id=ticket.ticket_id,
        verdict=Verdict.OVERTURNED,
        reasoning=reply.statement or reply.reasoning,
        chain=chain,
        challenger_model=client.name,
        logs_examined=len(logs.all_events()),
        files_examined=len(code_tools.list_files()),
    )


def contradicts_kb(ticket: Ticket, article_body: str) -> set[str]:
    """Terms the ticket and an article share, for the "spec vs behaviour" check.

    Used by the simulated challenger and available to real prompts as a hint. A
    customer describing the exact quantity an article promises — one hour, 50 MB,
    600 requests — is usually reporting that the promise is not being kept.
    """
    return tokenize(ticket.body) & tokenize(article_body)
