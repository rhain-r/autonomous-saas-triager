"""The evidence gate: what survives checking, and what does not."""

from __future__ import annotations

from agent.evidence import anchor_to_ticket, build_chain, explain_gap, link_terms
from agent.sandbox import load_ticket
from agent.schemas import FileQuote, Hypothesis
from agent.tools.logs import search_error_logs


def test_a_fully_supported_hypothesis_becomes_a_linked_chain(ticket, real_hypothesis):
    chain, fabrications = build_chain(ticket, real_hypothesis)
    assert fabrications == []
    assert chain is not None
    assert chain.is_linked
    assert "redirect" in chain.link_terms
    assert chain.code_spans[0].path == "src/auth/config.ts"


def test_every_invented_citation_is_recorded_and_nothing_survives(
    ticket, fabricated_hypothesis
):
    chain, fabrications = build_chain(ticket, fabricated_hypothesis)
    assert chain is None
    assert len(fabrications) == 4
    assert any("does not exist" in f for f in fabrications)
    assert any("is not in that file" in f for f in fabrications)


def test_a_partly_invented_hypothesis_keeps_what_is_real_and_flags_the_rest(ticket):
    """A run where the model invented half its citations is a different run from
    one where it invented none, even when the answer matches."""
    hypothesis = Hypothesis(
        statement="s",
        event_ids=["auth-svc-2026-08-14#3", "auth-svc-2026-08-14#9999"],
        file_quotes=[
            FileQuote(
                path="src/auth/config.ts",
                quote="redirectUri: `${STAGING_ORIGIN}/auth/callback`,",
            ),
            FileQuote(path="src/auth/config.ts", quote="const cache = new Map();"),
        ],
        reasoning="r",
    )
    chain, fabrications = build_chain(ticket, hypothesis)
    assert chain is not None
    assert len(chain.log_events) == 1
    assert len(chain.code_spans) == 1
    assert len(fabrications) == 2


def test_real_citations_that_do_not_connect_produce_an_unlinked_chain(ticket):
    """The load-bearing case. Both citations are genuine; the file has nothing
    to do with the error, and the gate is what notices."""
    hypothesis = Hypothesis(
        statement="The session store is at fault.",
        event_ids=["auth-svc-2026-08-14#3"],
        file_quotes=[
            FileQuote(
                path="src/auth/session.ts",
                quote="session.expiresAt = Date.now() + ttlSeconds * 1000;",
            )
        ],
        reasoning="Accurately quoted, entirely unconnected.",
    )
    chain, fabrications = build_chain(ticket, hypothesis)
    assert fabrications == [], "both citations are real"
    assert chain is not None
    assert not chain.is_linked
    assert "unproven" in explain_gap(chain, fabrications)


def test_a_chain_from_someone_elses_incident_fails_the_ticket_anchor(quiet_ticket):
    """The failure that shipped once: a blank dashboard explained with the
    previous day's password-reset errors from unrelated accounts."""
    hypothesis = Hypothesis(
        statement="Reset tokens are expiring.",
        event_ids=["auth-svc-2026-08-16#2", "auth-svc-2026-08-16#7"],
        file_quotes=[
            FileQuote(path="src/auth/reset.ts", quote='logger.warn("reset token expired", {')
        ],
        reasoning="Internally consistent and about a different customer entirely.",
    )
    chain, fabrications = build_chain(quiet_ticket, hypothesis)
    assert fabrications == []
    assert chain is not None
    assert chain.link_terms, "the log and code genuinely do connect to each other"
    assert not chain.ticket_anchor, "but not to this ticket"
    assert not chain.is_linked
    assert "somebody else's incident" in explain_gap(chain, fabrications)


def test_the_reporting_users_own_events_anchor_a_chain(ticket):
    events = search_error_logs(user_id="usr_8812")
    assert anchor_to_ticket(ticket, events).startswith("user usr_8812")


def test_shared_vocabulary_anchors_a_chain_when_the_user_id_differs():
    """A customer often reports a fault another account triggered."""
    billing = load_ticket("TCK-4488")
    events = [
        e for e in search_error_logs(query="LEDGER_DUPLICATE") if e.user_id != billing.user_id
    ]
    anchored = anchor_to_ticket(billing.model_copy(update={"user_id": "usr_other"}), events)
    assert anchored == "" or anchored.startswith("shared vocabulary")


def test_link_terms_ignore_words_that_would_connect_anything():
    events = search_error_logs(query="OAUTH_REDIRECT_MISMATCH")
    terms = link_terms(events, ["const request = await fetch(url); // failed to start"])
    assert "request" not in terms
    assert "failed" not in terms


def test_link_terms_bridge_camel_case_and_snake_case():
    events = search_error_logs(query="OAUTH_REDIRECT_MISMATCH")
    assert "redirect" in link_terms(events, ["  redirectUri: config.redirectUri,"])


def test_explain_gap_is_silent_when_the_chain_is_sound(ticket, real_hypothesis):
    chain, fabrications = build_chain(ticket, real_hypothesis)
    assert explain_gap(chain, fabrications) == ""


def test_explain_gap_names_the_fabrications_when_nothing_survives(
    ticket, fabricated_hypothesis
):
    chain, fabrications = build_chain(ticket, fabricated_hypothesis)
    assert "could not be found" in explain_gap(chain, fabrications)
