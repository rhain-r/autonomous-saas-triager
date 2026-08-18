"""The contracts, and what they refuse.

A schema is only worth having if you have confirmed what it rejects, so most of
these tests assert a `ValidationError` rather than a happy path.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.schemas import (
    ChallengeResult,
    Diagnosis,
    Disposition,
    EvidenceChain,
    Intent,
    JiraIssue,
    LogEvent,
    LogLevel,
    Patch,
    Severity,
    Verdict,
    tokenize,
)
from agent.tests.conftest import at


def _event(user: str = "usr_1", code: str = "OAUTH_REDIRECT_MISMATCH") -> LogEvent:
    return LogEvent(
        event_id="f#1",
        timestamp=at(9),
        level=LogLevel.ERROR,
        service="auth-svc",
        message="token exchange rejected",
        code=code,
        user_id=user,
        raw='2026-08-14T09:00:00.000Z ERROR auth-svc msg="token exchange rejected"',
        source_file="f.log",
        line_no=1,
    )


def _span(path: str = "src/auth/config.ts"):
    from agent.schemas import CodeSpan

    return CodeSpan(
        path=path,
        line_start=39,
        line_end=39,
        snippet="redirectUri: `${STAGING_ORIGIN}/auth/callback`,",
        content_sha="abc123",
    )


def _chain(*, link_terms: list[str] | None = None, anchor: str = "user usr_1") -> EvidenceChain:
    return EvidenceChain(
        symptom="cannot log in",
        log_events=[_event()],
        code_spans=[_span()],
        link_terms=["redirect", "uri"] if link_terms is None else link_terms,
        ticket_anchor=anchor,
        reasoning="because",
    )


# --- Tokenisation ------------------------------------------------------------


def test_tokenize_folds_identifier_styles_together():
    """The whole evidence gate rests on this: a log field must match its symbol."""
    assert tokenize("redirectUri") == tokenize("redirect_uri") == tokenize("REDIRECT_URI")


def test_tokenize_drops_words_that_would_link_anything_to_anything():
    assert "error" not in tokenize("error code")
    assert "the" not in tokenize("the redirect")


def test_tokenize_drops_short_fragments():
    assert tokenize("a bc def") == {"def"}


# --- Evidence chain ----------------------------------------------------------


def test_chain_needs_both_joints_to_be_linked():
    assert _chain().is_linked
    assert not _chain(link_terms=[]).is_linked, "no log-to-code overlap"
    assert not _chain(anchor="").is_linked, "no ticket-to-log anchor"


def test_chain_requires_at_least_one_log_event_and_one_code_span():
    with pytest.raises(ValidationError):
        EvidenceChain(
            symptom="x", log_events=[], code_spans=[_span()], reasoning="r"
        )
    with pytest.raises(ValidationError):
        EvidenceChain(
            symptom="x", log_events=[_event()], code_spans=[], reasoning="r"
        )


def test_blast_radius_counts_distinct_users():
    chain = EvidenceChain(
        symptom="x",
        log_events=[_event("usr_1"), _event("usr_2"), _event("usr_2")],
        code_spans=[_span()],
        link_terms=["redirect"],
        ticket_anchor="user usr_1",
        reasoning="r",
    )
    assert chain.blast_radius == 2


def test_link_strength_saturates():
    assert _chain(link_terms=["a"]).link_strength == 0.25
    assert _chain(link_terms=list("abcdefgh")).link_strength == 1.0


# --- Diagnosis ---------------------------------------------------------------


def test_escalation_without_a_chain_is_rejected():
    with pytest.raises(ValidationError, match="requires an EvidenceChain"):
        Diagnosis(
            ticket_id="T",
            intent=Intent.BUG,
            disposition=Disposition.ESCALATE,
            summary="s",
            chain=None,
        )


def test_escalation_on_an_unlinked_chain_is_rejected():
    """The load-bearing rule: real citations that do not connect are not evidence."""
    with pytest.raises(ValidationError, match="linked EvidenceChain"):
        Diagnosis(
            ticket_id="T",
            intent=Intent.BUG,
            disposition=Disposition.ESCALATE,
            summary="s",
            chain=_chain(link_terms=[]),
        )


def test_escalation_on_a_chain_from_someone_elses_incident_is_rejected():
    with pytest.raises(ValidationError, match="linked EvidenceChain"):
        Diagnosis(
            ticket_id="T",
            intent=Intent.BUG,
            disposition=Disposition.ESCALATE,
            summary="s",
            chain=_chain(anchor=""),
        )


def test_a_confirmed_bug_cannot_be_quietly_resolved():
    """The failure mode with no second chance."""
    with pytest.raises(ValidationError, match="cannot be resolved without escalation"):
        Diagnosis(
            ticket_id="T",
            intent=Intent.BUG,
            disposition=Disposition.RESOLVE,
            summary="s",
        )


def test_a_bug_may_be_held_for_a_human():
    diagnosis = Diagnosis(
        ticket_id="T", intent=Intent.BUG, disposition=Disposition.NEEDS_HUMAN, summary="s"
    )
    assert not diagnosis.is_auto_closing


def test_unknown_fields_are_an_error_not_a_shrug():
    with pytest.raises(ValidationError):
        Diagnosis(
            ticket_id="T",
            intent=Intent.HOW_TO,
            disposition=Disposition.RESOLVE,
            summary="s",
            confidence_level="very high",
        )


# --- Challenge ---------------------------------------------------------------


def test_an_overturn_must_be_quotable():
    with pytest.raises(ValidationError, match="requires a linked EvidenceChain"):
        ChallengeResult(ticket_id="T", verdict=Verdict.OVERTURNED, reasoning="I feel it")


def test_an_overturn_on_an_unlinked_chain_is_rejected():
    with pytest.raises(ValidationError):
        ChallengeResult(
            ticket_id="T",
            verdict=Verdict.OVERTURNED,
            reasoning="r",
            chain=_chain(link_terms=[]),
        )


def test_upholding_needs_no_evidence():
    result = ChallengeResult(ticket_id="T", verdict=Verdict.UPHELD, reasoning="looks correct")
    assert result.chain is None


# --- Patch -------------------------------------------------------------------


def test_a_rejected_patch_must_say_why():
    with pytest.raises(ValidationError, match="must record why"):
        Patch(path="src/a.ts", applies=False)


def test_an_applied_patch_must_carry_a_diff():
    with pytest.raises(ValidationError, match="must carry a diff"):
        Patch(path="src/a.ts", applies=True, diff="   ")


# --- Delivery ----------------------------------------------------------------


def test_jira_summary_is_length_bounded():
    with pytest.raises(ValidationError):
        JiraIssue(
            project="SUP",
            summary="x" * 300,
            description="d",
            priority="High",
            source_ticket="T",
        )


def test_severity_weights_are_ordered():
    from agent.schemas import SEVERITY_WEIGHT

    assert (
        SEVERITY_WEIGHT[Severity.CRITICAL]
        > SEVERITY_WEIGHT[Severity.HIGH]
        > SEVERITY_WEIGHT[Severity.MEDIUM]
        > SEVERITY_WEIGHT[Severity.LOW]
    )
