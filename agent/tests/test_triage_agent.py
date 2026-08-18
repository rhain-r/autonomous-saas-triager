"""The loop: routing, and the end-to-end behaviour of a full run."""

from __future__ import annotations

import pytest

from agent.evals.simulated import simulated_clients
from agent.sandbox import all_tickets, load_ticket
from agent.schemas import (
    Disposition,
    EvidenceChain,
    Intent,
    LogEvent,
    LogLevel,
    Severity,
    Verdict,
)
from agent.tests.conftest import at, stub_bundle
from agent.triage_agent import _Trace, gather, infer_severity, route, triage, triage_many


def _chain(*, linked: bool = True, users: int = 1, errored: bool = True) -> EvidenceChain:
    from agent.schemas import CodeSpan

    level = LogLevel.ERROR if errored else LogLevel.WARN
    events = [
        LogEvent(
            event_id=f"f#{i}",
            timestamp=at(9),
            level=level,
            service="auth-svc",
            message="m",
            code="C",
            user_id=f"usr_{i}",
            raw="raw",
            source_file="f.log",
            line_no=i + 1,
        )
        for i in range(users)
    ]
    return EvidenceChain(
        symptom="s",
        log_events=events,
        code_spans=[
            CodeSpan(path="src/a.ts", line_start=1, line_end=1, snippet="x", content_sha="s")
        ],
        link_terms=["redirect"] if linked else [],
        ticket_anchor="user usr_0" if linked else "",
        reasoning="r",
    )


# --- Routing -----------------------------------------------------------------


def test_a_bug_with_proof_escalates():
    assert route(Intent.BUG, _chain()) is Disposition.ESCALATE


def test_a_bug_without_proof_is_held_for_a_human_not_dropped():
    assert route(Intent.BUG, None) is Disposition.NEEDS_HUMAN
    assert route(Intent.BUG, _chain(linked=False)) is Disposition.NEEDS_HUMAN


def test_claiming_the_product_worked_also_needs_proof():
    """A closure is a claim about behaviour, and it is the claim with no
    second chance."""
    assert route(Intent.USER_ERROR, None) is Disposition.NEEDS_HUMAN
    assert route(Intent.USER_ERROR, _chain(linked=False)) is Disposition.NEEDS_HUMAN
    assert route(Intent.USER_ERROR, _chain()) is Disposition.RESOLVE


@pytest.mark.parametrize("intent", [Intent.HOW_TO, Intent.FEATURE_REQUEST])
def test_questions_and_wishes_are_answerable_without_evidence(intent):
    assert route(intent, None) is Disposition.RESOLVE


# --- Severity ----------------------------------------------------------------


def test_severity_comes_from_blast_radius_not_from_adjectives():
    assert infer_severity(Intent.BUG, _chain(users=5)) is Severity.CRITICAL
    assert infer_severity(Intent.BUG, _chain(users=2)) is Severity.HIGH
    assert infer_severity(Intent.BUG, _chain(users=1, errored=False)) is Severity.MEDIUM
    assert infer_severity(Intent.USER_ERROR, _chain(users=5)) is Severity.LOW


# --- Observation -------------------------------------------------------------


def test_gather_finds_the_reporters_failures_and_the_surrounding_context(settings):
    from agent.schemas import Classification

    ticket = load_ticket("TCK-3021")
    classification = Classification(
        intent=Intent.BUG, confidence=0.8, rationale="r", suspected_area="auth",
        search_terms=["oauth", "redirect"],
    )
    obs = gather(ticket, classification, settings, _Trace())

    assert any(e.code == "OAUTH_REDIRECT_MISMATCH" for e in obs.events)
    assert any(e.code == "OAUTH_START" for e in obs.events), "context INFO lines"
    assert "src/auth/config.ts" in obs.files


def test_gather_measures_blast_radius_across_accounts(settings):
    """"Is it just them?" is the question that makes severity honest."""
    from agent.schemas import Classification

    ticket = load_ticket("TCK-3021")
    classification = Classification(
        intent=Intent.BUG, confidence=0.8, rationale="r", suspected_area="auth", search_terms=[]
    )
    obs = gather(ticket, classification, settings, _Trace())
    affected = {e.user_id for e in obs.events if e.code == "OAUTH_REDIRECT_MISMATCH"}
    assert len(affected) > 1


def test_gather_reports_honest_emptiness_for_a_client_side_fault(settings):
    from agent.schemas import Classification

    ticket = load_ticket("TCK-7714")
    classification = Classification(
        intent=Intent.BUG, confidence=0.5, rationale="r", suspected_area="dashboard",
        search_terms=["dashboard", "blank"],
    )
    obs = gather(ticket, classification, settings, _Trace())
    assert obs.events == []


# --- End to end --------------------------------------------------------------


async def test_a_supported_bug_escalates_with_a_tracker_issue(settings, sink, ticket):
    clients = stub_bundle(
        {"intent": "bug", "confidence": 0.9, "rationale": "r", "suspected_area": "auth",
         "search_terms": ["oauth"]},
        {
            "statement": "redirectUri points at staging.",
            "event_ids": ["auth-svc-2026-08-14#3"],
            "file_quotes": [
                {"path": "src/auth/config.ts",
                 "quote": "redirectUri: `${STAGING_ORIGIN}/auth/callback`,"}
            ],
            "reasoning": "r",
        },
        patch={
            "path": "src/auth/config.ts",
            "old_text": "  redirectUri: `${STAGING_ORIGIN}/auth/callback`,",
            "new_text": "  redirectUri: callbackUrl(),",
            "rationale": "Use the resolved origin.",
            "test_hint": "callbackUrl() honours MERIDIAN_PUBLIC_ORIGIN.",
        },
    )
    report = await triage(ticket, clients, settings=settings, sink=sink)

    assert report.final_disposition is Disposition.ESCALATE
    assert report.jira_issue is not None
    assert report.receipt is not None
    assert report.patch is not None and report.patch.applies
    assert report.risk_score > 0
    assert report.diagnosis.fabrications == []


async def test_an_invented_root_cause_cannot_reach_the_tracker(settings, sink, ticket):
    """The headline guarantee. Every citation is plausible and none are real."""
    clients = stub_bundle(
        {"intent": "bug", "confidence": 0.95, "rationale": "r", "suspected_area": "auth",
         "search_terms": ["oauth"]},
        {
            "statement": "The session cache is misconfigured.",
            "event_ids": ["auth-svc-2026-08-14#9999"],
            "file_quotes": [
                {"path": "src/auth/config.ts", "quote": "const cache = new LruCache(512);"}
            ],
            "reasoning": "Confident and entirely fabricated.",
        },
    )
    report = await triage(ticket, clients, settings=settings, sink=sink)

    assert report.final_disposition is Disposition.NEEDS_HUMAN
    assert report.jira_issue is None
    assert report.receipt is None
    assert len(report.diagnosis.fabrications) == 2
    assert any("could not be found" in n for n in report.model_notes)


async def test_accurate_citations_that_do_not_connect_cannot_reach_the_tracker(
    settings, sink, ticket
):
    """Both quotes are real; the file is innocent. Nothing is fabricated and it
    still must not become an escalation."""
    clients = stub_bundle(
        {"intent": "bug", "confidence": 0.9, "rationale": "r", "suspected_area": "auth",
         "search_terms": ["oauth"]},
        {
            "statement": "Session TTL is at fault.",
            "event_ids": ["auth-svc-2026-08-14#3"],
            "file_quotes": [
                {"path": "src/auth/session.ts",
                 "quote": "session.expiresAt = Date.now() + ttlSeconds * 1000;"}
            ],
            "reasoning": "r",
        },
    )
    report = await triage(ticket, clients, settings=settings, sink=sink)

    assert report.diagnosis.fabrications == []
    assert report.final_disposition is Disposition.NEEDS_HUMAN
    assert report.jira_issue is None


async def test_an_auto_close_without_a_challenger_is_downgraded_to_a_human(
    settings, sink
):
    clients = stub_bundle(
        {"intent": "how_to", "confidence": 0.9, "rationale": "r", "suspected_area": None,
         "search_terms": ["password"]},
        {"statement": "s", "event_ids": [], "file_quotes": [], "reasoning": "r"},
    )
    report = await triage(load_ticket("TCK-1102"), clients, settings=settings, sink=sink)

    assert report.final_disposition is Disposition.NEEDS_HUMAN
    assert any("refusing to auto-close" in n for n in report.model_notes)


async def test_disabling_the_challenge_requirement_permits_an_unguarded_close(
    settings, sink
):
    relaxed = settings.model_copy(update={"require_challenge": False})
    clients = stub_bundle(
        {"intent": "how_to", "confidence": 0.9, "rationale": "r", "suspected_area": None,
         "search_terms": ["password"]},
        {"statement": "s", "event_ids": [], "file_quotes": [], "reasoning": "r"},
    )
    report = await triage(load_ticket("TCK-1102"), clients, settings=relaxed, sink=sink)

    assert report.final_disposition is Disposition.RESOLVE
    assert report.customer_reply


async def test_an_overturned_resolution_becomes_an_escalation(settings, sink):
    """The silent-closure rescue, end to end."""
    clients = stub_bundle(
        {"intent": "how_to", "confidence": 0.9, "rationale": "reads like a reset question",
         "suspected_area": "auth", "search_terms": ["reset", "password"]},
        {"statement": "Routine reset question.", "event_ids": [], "file_quotes": [],
         "reasoning": "r"},
        patch={
            "path": "src/auth/reset.ts",
            "old_text": "  if (age > RESET_TOKEN_TTL_SECONDS) {",
            "new_text": "  if (age > RESET_TOKEN_TTL_SECONDS * 1000) {",
            "rationale": "Milliseconds compared against seconds.",
            "test_hint": "A token issued 90 seconds ago must still verify.",
        },
        challenge={
            "verdict": "overturned",
            "reasoning": "Tokens expire in seconds against a documented hour.",
            "statement": "verifyResetToken compares ms against s.",
            "event_ids": ["auth-svc-2026-08-16#2", "auth-svc-2026-08-16#4"],
            "file_quotes": [
                {"path": "src/auth/reset.ts", "quote": 'logger.warn("reset token expired", {'}
            ],
        },
    )
    report = await triage(load_ticket("TCK-6033"), clients, settings=settings, sink=sink)

    assert report.was_overturned
    assert report.final_disposition is Disposition.ESCALATE
    assert report.diagnosis.intent is Intent.BUG
    assert report.jira_issue is not None
    assert report.patch is not None and report.patch.applies


async def test_an_upheld_resolution_closes_the_ticket(settings, sink):
    clients = stub_bundle(
        {"intent": "user_error", "confidence": 0.9, "rationale": "over a published limit",
         "suspected_area": "exports", "search_terms": ["export", "limit"]},
        {
            "statement": "The export exceeded the documented ceiling.",
            "event_ids": ["exports-svc-2026-08-15#2"],
            "file_quotes": [
                {"path": "src/exports/uploader.ts",
                 "quote": 'logger.warn("export rejected: over published limit", {'}
            ],
            "reasoning": "r",
        },
        challenge={"verdict": "upheld", "reasoning": "320 MB against a 50 MB ceiling."},
    )
    report = await triage(load_ticket("TCK-5210"), clients, settings=settings, sink=sink)

    assert report.final_disposition is Disposition.RESOLVE
    assert report.challenge is not None and report.challenge.verdict is Verdict.UPHELD
    assert report.jira_issue is None
    assert report.risk_score == 0.0


async def test_a_failed_challenge_blocks_the_close(settings, sink):
    clients = stub_bundle(
        {"intent": "user_error", "confidence": 0.9, "rationale": "r",
         "suspected_area": "exports", "search_terms": ["export"]},
        {
            "statement": "s",
            "event_ids": ["exports-svc-2026-08-15#2"],
            "file_quotes": [
                {"path": "src/exports/uploader.ts",
                 "quote": 'logger.warn("export rejected: over published limit", {'}
            ],
            "reasoning": "r",
        },
        challenge={"verdict": "needs_human", "reasoning": "Could not complete the check."},
    )
    report = await triage(load_ticket("TCK-5210"), clients, settings=settings, sink=sink)
    assert report.final_disposition is Disposition.NEEDS_HUMAN


async def test_the_trace_records_every_tool_call(settings, sink, ticket):
    clients = stub_bundle(
        {"intent": "bug", "confidence": 0.9, "rationale": "r", "suspected_area": "auth",
         "search_terms": ["oauth"]},
        {"statement": "s", "event_ids": [], "file_quotes": [], "reasoning": "r"},
    )
    report = await triage(ticket, clients, settings=settings, sink=sink)
    tools_used = {step.tool.value for step in report.trace}
    assert {"search_error_logs", "search_codebase", "git_log", "search_docs"} <= tools_used
    assert [s.index for s in report.trace] == list(range(len(report.trace)))


# --- The whole queue ---------------------------------------------------------


async def test_the_full_queue_runs_offline_and_closes_and_escalates(settings, sink):
    """A triager that escalates everything is as useless as one that closes
    everything, so both outcomes must appear."""
    reports = await triage_many(
        list(all_tickets()), simulated_clients(), settings=settings, sink=sink
    )
    decisions = {r.final_disposition for r in reports}
    assert Disposition.ESCALATE in decisions
    assert Disposition.RESOLVE in decisions
    assert len(reports) == len(all_tickets())


async def test_no_escalation_in_a_full_run_carries_an_unlinked_chain(settings, sink):
    reports = await triage_many(
        list(all_tickets()), simulated_clients(), settings=settings, sink=sink
    )
    for report in reports:
        if report.final_disposition is Disposition.ESCALATE:
            assert report.diagnosis.chain is not None
            assert report.diagnosis.chain.is_linked
            assert report.jira_issue is not None
