"""Scoring, tracker payloads, rendering, and the eval harness itself."""

from __future__ import annotations

import json

import pytest

from agent.evals.run import CONFIGURATIONS, run_configuration
from agent.evals.scoring import load_keys
from agent.patcher import verify_patch
from agent.reporter import build_jira_issue, queue_summary, risk_score, save_report, to_markdown
from agent.sandbox import all_tickets, load_ticket
from agent.schemas import Diagnosis, Disposition, Intent, PatchProposal, Severity
from agent.tests.test_triage_agent import _chain
from agent.tools.tracker import FileSink, build_sink


def _diagnosis(**kwargs) -> Diagnosis:
    base = {
        "ticket_id": "TCK-3021",
        "intent": Intent.BUG,
        "disposition": Disposition.ESCALATE,
        "severity": Severity.HIGH,
        "summary": "redirectUri points at staging.",
        "chain": _chain(users=3),
    }
    base.update(kwargs)
    return Diagnosis(**base)


# --- Risk scoring ------------------------------------------------------------


def test_risk_rises_with_severity_and_with_blast_radius():
    low = risk_score(_diagnosis(severity=Severity.LOW, chain=_chain(users=1)), Disposition.ESCALATE)
    same_severity_wider = risk_score(
        _diagnosis(severity=Severity.LOW, chain=_chain(users=5)), Disposition.ESCALATE
    )
    higher_severity = risk_score(
        _diagnosis(severity=Severity.CRITICAL, chain=_chain(users=1)), Disposition.ESCALATE
    )
    assert low < same_severity_wider < higher_severity


def test_risk_is_bounded():
    assert (
        risk_score(_diagnosis(severity=Severity.CRITICAL, chain=_chain(users=99)),
                   Disposition.ESCALATE)
        <= 100.0
    )


def test_anything_not_escalated_scores_zero():
    """A well-answered how-to must not outrank a real fault in the queue."""
    for disposition in (Disposition.RESOLVE, Disposition.NEEDS_HUMAN):
        assert risk_score(_diagnosis(), disposition) == 0.0


# --- Tracker payload ---------------------------------------------------------


def test_the_issue_carries_every_verified_locator(settings, ticket):
    issue = build_jira_issue(ticket, _diagnosis(), None, settings)
    assert ticket.ticket_id in issue.summary
    assert "src/a.ts:1" in issue.description
    assert "f.log:1" in issue.description
    assert "Shared diagnostic terms" in issue.description


def test_the_issue_names_rejected_claims_rather_than_hiding_them(settings, ticket):
    diagnosis = _diagnosis(fabrications=["quote attributed to src/x.ts is not in that file"])
    issue = build_jira_issue(ticket, diagnosis, None, settings)
    assert "Unverified claims (rejected)" in issue.description
    assert "unverified-claims-present" in issue.labels


def test_a_high_risk_escalation_is_labelled_for_paging(settings, ticket):
    issue = build_jira_issue(
        ticket, _diagnosis(severity=Severity.CRITICAL, chain=_chain(users=5)), None, settings
    )
    assert "page-oncall" in issue.labels


def test_a_rejected_patch_is_reported_not_swallowed(settings, ticket):
    patch = verify_patch(
        PatchProposal(
            path="src/auth/reset.ts",
            old_text="this line is not in the file",
            new_text="x",
            rationale="r",
        )
    )
    issue = build_jira_issue(ticket, _diagnosis(), patch, settings)
    assert "was rejected" in issue.description
    assert "has-candidate-patch" not in issue.labels


def test_an_applied_patch_is_attached_as_a_diff(settings, ticket):
    patch = verify_patch(
        PatchProposal(
            path="src/auth/reset.ts",
            old_text="  if (age > RESET_TOKEN_TTL_SECONDS) {",
            new_text="  if (age > RESET_TOKEN_TTL_SECONDS * 1000) {",
            rationale="Milliseconds against seconds.",
            test_hint="A token issued 90 seconds ago must still verify.",
        )
    )
    issue = build_jira_issue(ticket, _diagnosis(), patch, settings)
    assert "{code:diff}" in issue.description
    assert "has-candidate-patch" in issue.labels


def test_components_are_derived_from_the_cited_paths(settings, ticket):
    from agent.reporter import component_for

    assert component_for(["src/auth/config.ts"]) == ["authentication"]
    assert component_for(["src/billing/webhook.ts", "src/api/client.ts"]) == [
        "billing",
        "public-api",
    ]


# --- Sinks -------------------------------------------------------------------


def test_the_file_sink_writes_the_payload_a_real_call_would_carry(settings, tmp_path, ticket):
    sink = FileSink(tmp_path / "out")
    receipt = sink.create(build_jira_issue(ticket, _diagnosis(), None, settings))
    written = json.loads(next((tmp_path / "out").glob("*.json")).read_text(encoding="utf-8"))
    assert written["issue_key"] == receipt.issue_key
    assert written["fields"]["source_ticket"] == ticket.ticket_id


def test_live_escalation_without_a_token_refuses_rather_than_dropping_it(settings):
    with pytest.raises(RuntimeError, match="refusing to silently drop"):
        build_sink(settings, live=True)


def test_the_default_sink_is_never_the_live_one(settings):
    assert build_sink(settings).name == "file"


# --- Rendering ---------------------------------------------------------------


async def test_markdown_contains_the_evidence_and_the_trace(settings, sink, ticket):
    from agent.evals.simulated import simulated_clients
    from agent.triage_agent import triage

    report = await triage(ticket, simulated_clients(), settings=settings, sink=sink)
    text = to_markdown(report)
    assert "## Verified evidence" in text
    assert "## Tool trace" in text
    assert "src/auth/config.ts" in text


async def test_a_report_round_trips_through_json(settings, sink, ticket, tmp_path):
    from agent.evals.simulated import simulated_clients
    from agent.schemas import TriageReport
    from agent.triage_agent import triage

    report = await triage(ticket, simulated_clients(), settings=settings, sink=sink)
    path = save_report(report, tmp_path / "reports")
    restored = TriageReport.model_validate_json(path.read_text(encoding="utf-8"))
    assert restored.ticket_id == report.ticket_id
    assert restored.final_disposition is report.final_disposition


def test_queue_summary_counts_every_outcome():
    summary = queue_summary([])
    assert set(summary) >= {"tickets", "escalated", "resolved", "needs_human", "overturned"}


# --- The eval harness --------------------------------------------------------


def test_every_ticket_has_an_answer_key():
    assert {t.ticket_id for t in all_tickets()} == set(load_keys())


def test_answer_keys_are_internally_consistent():
    for key in load_keys().values():
        if key.disposition is Disposition.ESCALATE:
            assert key.intent is Intent.BUG
            assert key.root_cause_paths, "an escalation must name the file it blames"
        else:
            assert not key.root_cause_paths


def test_the_traps_are_present_and_distinct():
    traps = {k.trap for k in load_keys().values() if k.trap}
    assert traps == {
        "false escalation",
        "silent closure",
        "misattribution",
        "no evidence exists",
        "single-account defect",
    }


def test_the_challenger_reduces_silent_closures_against_the_unguarded_baseline(tmp_path):
    """The claim the architecture makes, measured. If this stops holding, the
    challenger is not paying for itself."""
    unguarded = run_configuration(*CONFIGURATIONS[0], sink_dir=tmp_path)
    challenged = run_configuration(*CONFIGURATIONS[1], sink_dir=tmp_path)
    assert unguarded.silent_closures > challenged.silent_closures
    assert challenged.correct_overturns == challenged.overturns, "no overturn on a non-defect"


def test_the_eval_never_escalates_without_a_linked_chain(tmp_path):
    for config in CONFIGURATIONS:
        metrics = run_configuration(*config, sink_dir=tmp_path)
        for outcome in metrics.outcomes:
            if outcome.actual is Disposition.ESCALATE:
                assert outcome.cited_paths, f"{outcome.ticket_id} escalated citing nothing"


def test_the_no_evidence_ticket_is_never_confidently_answered(tmp_path):
    """TCK-7714 has no server-side evidence. Any confident answer is wrong."""
    for config in CONFIGURATIONS:
        metrics = run_configuration(*config, sink_dir=tmp_path)
        outcome = next(o for o in metrics.outcomes if o.ticket_id == "TCK-7714")
        assert outcome.actual is Disposition.NEEDS_HUMAN


def test_the_load_bearing_ticket_is_attributed_to_the_right_file(tmp_path):
    """TCK-3021 is the case where logs and code genuinely connect. If the
    pipeline cannot get this one right, none of the gates matter."""
    metrics = run_configuration(*CONFIGURATIONS[-1], sink_dir=tmp_path)
    outcome = next(o for o in metrics.outcomes if o.ticket_id == "TCK-3021")
    assert outcome.actual is Disposition.ESCALATE
    assert outcome.attribution == "correct"


def test_the_misattribution_trap_is_recorded_honestly(tmp_path):
    """TCK-9302's tempting wrong answer is src/api/rate_limit.ts. Whatever the
    pipeline does here, the scorer must report it rather than round it up."""
    metrics = run_configuration(*CONFIGURATIONS[-1], sink_dir=tmp_path)
    outcome = next(o for o in metrics.outcomes if o.ticket_id == "TCK-9302")
    assert outcome.attribution in {"correct", "wrong"}
    if outcome.attribution == "wrong":
        assert "src/api/client.ts" not in outcome.cited_paths


def test_a_ticket_id_that_does_not_exist_is_an_error_not_an_empty_run():
    with pytest.raises(KeyError, match="unknown ticket"):
        load_ticket("TCK-0000")
