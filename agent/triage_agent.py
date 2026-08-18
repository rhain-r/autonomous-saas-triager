"""The triage loop: classify, gather, hypothesise, verify, route, deliver.

Two questions get asked about every ticket, by two different mechanisms, because
they are genuinely different questions:

**Is the blamed code actually implicated?** Answered deterministically, in
`agent.evidence`, by checking every citation against the real files and then
intersecting the error vocabulary with the code vocabulary. No model gets a vote.

**Was "not a defect" the right call?** Answered adversarially, in
`agent.challenger`, by a second model from a different lab whose only job is to
prove the resolution wrong before the ticket closes.

Neither substitutes for the other. The evidence gate cannot tell a defect from
correct behaviour — the export-size rejection in the sandbox links perfectly to
the code that emits it, and that code is working exactly as designed. The
challenger cannot tell a real citation from an invented one; it hands its own
findings back through the same gate. Together they cover the two ways an L1 agent
loses money: escalating a ghost, and closing a real bug.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agent import code_tools
from agent.challenger import challenge_resolution
from agent.config import Settings, get_settings, load_prompt
from agent.evidence import build_chain, explain_gap
from agent.llm import ModelClient, ModelError, complete_structured
from agent.patcher import verify_patch
from agent.reporter import build_jira_issue, risk_score
from agent.schemas import (
    Classification,
    CodeMatch,
    Commit,
    Diagnosis,
    Disposition,
    EvidenceChain,
    Hypothesis,
    Intent,
    KbHit,
    LogEvent,
    Patch,
    PatchProposal,
    Severity,
    Ticket,
    ToolName,
    TriageReport,
    TriageStep,
    Verdict,
)
from agent.tools import kb, logs
from agent.tools.tracker import TrackerSink, build_sink

# Which part of the source tree each product area maps to. Wrong here just means
# a wider search, so the mapping is deliberately shallow.
AREA_PREFIX: dict[str, str] = {
    "auth": "src/auth",
    "billing": "src/billing",
    "api": "src/api",
    "exports": "src/exports",
    "dashboard": "src",
    "account": "src/auth",
    "ui": "src",
}


@dataclass(slots=True)
class AgentClients:
    """The four model roles. Any of them may be a stub or a simulation."""

    classifier: ModelClient
    investigator: ModelClient
    patcher: ModelClient
    challenger: ModelClient | None = None


class _Trace:
    """Records every tool call so a run can be audited after the fact."""

    def __init__(self) -> None:
        self.steps: list[TriageStep] = []

    def add(self, tool: ToolName, arguments: dict[str, str], count: int, note: str = "") -> None:
        self.steps.append(
            TriageStep(
                index=len(self.steps),
                tool=tool,
                arguments={k: str(v) for k, v in arguments.items() if v not in (None, "")},
                result_count=count,
                note=note,
            )
        )


@dataclass(slots=True)
class Observations:
    """Everything the tools found, before any model has interpreted it."""

    events: list[LogEvent]
    matches: list[CodeMatch]
    commits: list[Commit]
    kb_hits: list[KbHit]
    files: dict[str, str]


# --- Stage 1: classification -------------------------------------------------


async def classify(client: ModelClient, ticket: Ticket, settings: Settings) -> Classification:
    return await complete_structured(
        client,
        load_prompt("classifier"),
        ticket.render(),
        Classification,
        max_tokens=settings.max_output_tokens,
    )


# --- Stage 2: observation ----------------------------------------------------


def gather(
    ticket: Ticket,
    classification: Classification,
    settings: Settings,
    trace: _Trace,
) -> Observations:
    """Run the tools. Deterministic, so it costs nothing and always runs.

    The order matters. Logs come first because the error signature they carry is
    what makes the code search precise; searching the tree for the customer's own
    words instead returns whatever file happens to share their vocabulary.
    """
    query = " ".join(classification.search_terms) or f"{ticket.subject} {ticket.body}"

    events = logs.search_error_logs(
        user_id=ticket.user_id,
        around=ticket.created_at,
        window_hours=settings.log_window_hours,
        limit=settings.max_log_events,
    )
    trace.add(
        ToolName.SEARCH_ERROR_LOGS,
        {"user_id": ticket.user_id or "", "window_hours": settings.log_window_hours},
        len(events),
    )

    # No failures for this user in the window. Widen to the whole estate before
    # concluding there is nothing — a shared-account customer often reports a
    # fault that another user id actually triggered.
    if not events:
        events = logs.search_error_logs(
            around=ticket.created_at,
            window_hours=settings.log_window_hours,
            query=query,
            limit=settings.max_log_events,
        )
        trace.add(
            ToolName.SEARCH_ERROR_LOGS,
            {"query": query, "scope": "all-users"},
            len(events),
            "widened after no per-user failures",
        )

    # Neighbouring INFO lines carry the half of the story the error omits.
    if events:
        anchor = events[0]
        context = logs.read_log_window(anchor.source_file, anchor.line_no)
        trace.add(
            ToolName.READ_LOG_WINDOW,
            {"source_file": anchor.source_file, "line_no": anchor.line_no},
            len(context),
        )
        known = {e.event_id for e in events}
        events = sorted(
            events + [e for e in context if e.event_id not in known],
            key=lambda e: (e.timestamp, e.line_no),
        )[: settings.max_log_events]

    # "Is it just them?" — the question a real L1 engineer asks next, and the
    # only way severity comes out right. Blast radius is what separates one
    # account's misconfiguration from an incident, and it is invisible inside a
    # single user's slice. Scoped to the error codes already seen, so this
    # widens the *count*, not the hypothesis.
    codes = [e.code for e in events if e.code and e.level.is_failure]
    if codes:
        for signature in dict.fromkeys(codes):
            others = logs.search_error_logs(
                around=ticket.created_at,
                window_hours=settings.log_window_hours,
                query=signature,
                limit=settings.max_log_events,
            )
            known = {e.event_id for e in events}
            fresh = [e for e in others if e.event_id not in known]
            if fresh:
                events = sorted(
                    events + fresh, key=lambda e: (e.timestamp, e.source_file, e.line_no)
                )[: settings.max_log_events]
            trace.add(
                ToolName.SEARCH_ERROR_LOGS,
                {"query": signature, "scope": "blast-radius"},
                len(others),
                f"{len({e.user_id for e in others if e.user_id})} distinct user(s)",
            )

    # Search the code for what the logs actually said, not what the customer said.
    signature_terms = " ".join(dict.fromkeys(e.code or "" for e in events if e.code))
    code_query = f"{signature_terms} {query}".strip()
    prefix = AREA_PREFIX.get(classification.suspected_area or ticket.product_area or "", "")
    matches = code_tools.search_codebase(
        code_query, path_prefix=prefix, max_results=settings.max_code_matches
    )
    trace.add(ToolName.SEARCH_CODEBASE, {"query": code_query, "path_prefix": prefix}, len(matches))

    # Fall back to the whole tree if the area guess found nothing.
    if not matches and prefix:
        matches = code_tools.search_codebase(code_query, max_results=settings.max_code_matches)
        trace.add(
            ToolName.SEARCH_CODEBASE,
            {"query": code_query, "path_prefix": ""},
            len(matches),
            "widened after no matches in the suspected area",
        )

    commits = code_tools.git_log(prefix, limit=6)
    trace.add(ToolName.GIT_LOG, {"path_prefix": prefix}, len(commits))

    kb_hits = kb.search_docs(f"{ticket.subject} {ticket.body}")
    trace.add(ToolName.SEARCH_DOCS, {"query": ticket.subject}, len(kb_hits))

    files: dict[str, str] = {}
    for path in _top_paths(matches, limit=3):
        files[path] = code_tools.read_file(path, 1, settings.max_file_lines)
        trace.add(ToolName.READ_FILE, {"path": path}, 1)

    return Observations(events, matches, commits, kb_hits, files)


def _top_paths(matches: list[CodeMatch], limit: int) -> list[str]:
    """Distinct files from the search hits, best-scoring first."""
    ordered: dict[str, None] = {}
    for match in matches:
        ordered.setdefault(match.path, None)
        if len(ordered) >= limit:
            break
    return list(ordered)


def render_observations(obs: Observations) -> str:
    """The observation payload as handed to the investigator."""
    parts: list[str] = []

    if obs.events:
        lines = "\n".join(f"[{e.event_id}] {e.raw}" for e in obs.events)
        parts.append(f"## Log events\n\n{lines}")
    else:
        parts.append(
            "## Log events\n\nNone. No failure was recorded for this user in the "
            "window around the ticket. Treat the absence as meaningful."
        )

    if obs.matches:
        hits = "\n".join(f"{m.path}:{m.line_no}: {m.line.strip()}" for m in obs.matches[:15])
        parts.append(f"## Code search hits\n\n{hits}")

    for path, contents in obs.files.items():
        parts.append(f"## File: {path}\n\n```\n{contents}\n```")

    if obs.commits:
        history = "\n".join(
            f"{c.sha} {c.authored_at:%Y-%m-%d} {c.author}: {c.subject} "
            f"[{', '.join(c.files)}]"
            for c in obs.commits
        )
        parts.append(f"## Recent commits\n\n{history}")

    if obs.kb_hits:
        articles = "\n".join(f"{h.article_id} {h.title} (score {h.score:.1f})" for h in obs.kb_hits)
        parts.append(f"## Help-centre matches\n\n{articles}")

    return "\n\n".join(parts)


# --- Stage 3: hypothesis and verification ------------------------------------


async def investigate(
    client: ModelClient,
    ticket: Ticket,
    obs: Observations,
    settings: Settings,
) -> Hypothesis:
    prompt = f"{ticket.render()}\n\n{render_observations(obs)}"
    return await complete_structured(
        client,
        load_prompt("investigator"),
        prompt,
        Hypothesis,
        max_tokens=settings.max_output_tokens,
    )


def route(
    intent: Intent,
    chain: EvidenceChain | None,
) -> Disposition:
    """Turn an intent and a verified chain into a decision.

    Two of the four intents make a claim *about the product*, and both are held
    to the same standard:

    - `bug` says the product is broken. Escalating needs a model that believes
      it and evidence that survives checking. Belief without evidence is
      `needs_human` — the ticket stays open and a person looks, which is the
      right answer when the agent is confident and cannot prove it.
    - `user_error` says the product worked correctly. That is equally a claim
      about behaviour, and it closes the ticket, so it needs the same linked
      chain. "Nothing in the logs, but I'm sure it's fine" is how a real defect
      gets closed. Without evidence this is `needs_human` too.

    The other two make no claim about behaviour. `how_to` points at a help
    article and `feature_request` records a wish; both are legitimately
    answerable from the knowledge base alone.
    """
    proven = chain is not None and chain.is_linked

    if intent is Intent.BUG:
        return Disposition.ESCALATE if proven else Disposition.NEEDS_HUMAN
    if intent is Intent.USER_ERROR:
        return Disposition.RESOLVE if proven else Disposition.NEEDS_HUMAN
    return Disposition.RESOLVE


def infer_severity(intent: Intent, chain: EvidenceChain | None) -> Severity:
    """Severity from blast radius and failure level, not from the model.

    A model asked to rate severity rates the customer's adjectives. The log
    events are already in hand and they answer the question directly: how many
    distinct users hit this, and did anything actually error.
    """
    if intent is not Intent.BUG or chain is None:
        return Severity.LOW
    affected = chain.blast_radius
    errored = any(e.level.name == "ERROR" for e in chain.log_events)
    if affected >= 4 and errored:
        return Severity.CRITICAL
    if affected >= 2 or errored:
        return Severity.HIGH
    return Severity.MEDIUM


# --- Stage 4: patch ----------------------------------------------------------


async def draft_patch(
    client: ModelClient,
    diagnosis: Diagnosis,
    settings: Settings,
) -> Patch | None:
    """Ask for a candidate fix for the primary cited file, then verify it."""
    if diagnosis.chain is None or not diagnosis.chain.code_spans:
        return None

    span = diagnosis.chain.code_spans[0]
    contents = code_tools.read_file(span.path, 1, settings.max_file_lines)
    prompt = (
        f"Root cause: {diagnosis.summary}\n\n"
        f"Evidence:\n{diagnosis.chain.render()}\n\n"
        f"## File: {span.path}\n\n```\n{contents}\n```"
    )

    try:
        proposal = await complete_structured(
            client,
            load_prompt("patcher"),
            prompt,
            PatchProposal,
            max_tokens=settings.max_output_tokens,
        )
    except ModelError as exc:
        return Patch(
            path=span.path,
            applies=False,
            rejection_reason=f"patch drafting failed: {exc}",
        )

    return verify_patch(proposal)


# --- Stage 5: reply ----------------------------------------------------------


async def draft_reply(
    client: ModelClient,
    ticket: Ticket,
    diagnosis: Diagnosis,
    disposition: Disposition,
    reference: str | None,
    settings: Settings,
) -> str:
    context = [
        ticket.render(),
        f"\nDecision: {disposition.value}",
        f"Verified summary: {diagnosis.summary}",
    ]
    if reference:
        context.append(f"Reference to quote: {reference}")
    if diagnosis.kb_hits:
        articles = "\n".join(f"- {h.title} ({h.path}): {h.excerpt}" for h in diagnosis.kb_hits)
        context.append(f"Help-centre articles available:\n{articles}")

    reply = await client.complete(
        load_prompt("reply"), "\n".join(context), max_tokens=settings.max_output_tokens
    )
    return reply.strip()


# --- The loop ----------------------------------------------------------------


async def triage(
    ticket: Ticket,
    clients: AgentClients,
    *,
    settings: Settings | None = None,
    sink: TrackerSink | None = None,
) -> TriageReport:
    """Run one ticket end to end."""
    settings = settings or get_settings()
    sink = sink or build_sink(settings)
    trace = _Trace()
    notes: list[str] = []

    classification = await classify(clients.classifier, ticket, settings)
    obs = gather(ticket, classification, settings, trace)
    hypothesis = await investigate(clients.investigator, ticket, obs, settings)

    chain, fabrications = build_chain(ticket, hypothesis, commits=obs.commits)
    if fabrications:
        notes.append(f"{len(fabrications)} claimed citation(s) did not resolve")
    gap = explain_gap(chain, fabrications)
    if gap:
        notes.append(gap)

    disposition = route(classification.intent, chain)
    diagnosis = Diagnosis(
        ticket_id=ticket.ticket_id,
        intent=classification.intent,
        disposition=disposition,
        severity=infer_severity(classification.intent, chain),
        confidence=classification.confidence,
        summary=hypothesis.statement,
        chain=chain,
        kb_hits=obs.kb_hits,
        fabrications=fabrications,
        agent_name=clients.investigator.name,
    )

    # The challenger only ever looks at auto-closes. An escalation already has a
    # human in its future; a resolution does not, which is the whole asymmetry.
    challenge = None
    final = disposition
    if diagnosis.is_auto_closing:
        if clients.challenger is None:
            # An unchallenged auto-close is the one outcome with no safety net,
            # so by default a missing challenger downgrades to a human rather
            # than proceeding on the investigator's word alone.
            if settings.require_challenge:
                final = Disposition.NEEDS_HUMAN
                notes.append("no challenger configured; refusing to auto-close unchallenged")
            else:
                notes.append("closed without a challenge (require_challenge is off)")
        else:
            challenge = await challenge_resolution(clients.challenger, ticket, diagnosis, settings)
            if challenge.verdict is Verdict.OVERTURNED:
                final = Disposition.ESCALATE
                diagnosis = diagnosis.model_copy(
                    update={
                        "intent": Intent.BUG,
                        "disposition": Disposition.ESCALATE,
                        "chain": challenge.chain,
                        "severity": infer_severity(Intent.BUG, challenge.chain),
                        "summary": challenge.reasoning,
                    }
                )
                notes.append("resolution overturned by the challenger")
            elif challenge.verdict is Verdict.NEEDS_HUMAN:
                final = Disposition.NEEDS_HUMAN
                notes.append("challenger could not complete; refusing to auto-close")

    if classification.confidence < settings.challenge_confidence_floor:
        notes.append(
            f"classifier confidence {classification.confidence:.2f} is below the "
            f"{settings.challenge_confidence_floor:.2f} floor"
        )

    patch = None
    issue = None
    receipt = None
    if final is Disposition.ESCALATE:
        patch = await draft_patch(clients.patcher, diagnosis, settings)
        issue = build_jira_issue(ticket, diagnosis, patch, settings)
        receipt = sink.create(issue)

    reply = await draft_reply(
        clients.investigator,
        ticket,
        diagnosis,
        final,
        receipt.issue_key if receipt else None,
        settings,
    )

    return TriageReport(
        ticket_id=ticket.ticket_id,
        classification=classification,
        diagnosis=diagnosis,
        challenge=challenge,
        final_disposition=final,
        patch=patch,
        jira_issue=issue,
        receipt=receipt,
        customer_reply=reply,
        risk_score=risk_score(diagnosis, final),
        trace=trace.steps,
        model_notes=notes,
    )


async def triage_many(
    tickets: list[Ticket],
    clients: AgentClients,
    *,
    settings: Settings | None = None,
    sink: TrackerSink | None = None,
) -> list[TriageReport]:
    """Run a queue concurrently, bounded by `ticket_concurrency`."""
    settings = settings or get_settings()
    semaphore = asyncio.Semaphore(settings.ticket_concurrency)

    async def one(ticket: Ticket) -> TriageReport:
        async with semaphore:
            return await triage(ticket, clients, settings=settings, sink=sink)

    return list(await asyncio.gather(*(one(t) for t in tickets)))
