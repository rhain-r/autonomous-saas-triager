"""The adversarial check on closing a ticket.

The rule under test throughout: a challenger that cannot complete, cannot quote,
or cannot connect its evidence must never read as agreement.
"""

from __future__ import annotations

import json

import pytest

from agent.challenger import challenge_resolution
from agent.llm import StubClient
from agent.sandbox import load_ticket
from agent.schemas import Diagnosis, Disposition, Intent, Verdict


class ExplodingClient:
    """A provider that fails. The most important client in this file."""

    name = "exploding"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def complete(self, system: str, prompt: str, *, max_tokens: int) -> str:
        raise self._exc


def _resolution(ticket_id: str, intent: Intent = Intent.USER_ERROR) -> Diagnosis:
    return Diagnosis(
        ticket_id=ticket_id,
        intent=intent,
        disposition=Disposition.RESOLVE,
        summary="Working as documented.",
        confidence=0.9,
    )


async def test_a_challenger_that_crashes_is_not_agreement(settings):
    ticket = load_ticket("TCK-6033")
    result = await challenge_resolution(
        ExplodingClient(TimeoutError("upstream timed out")),
        ticket,
        _resolution("TCK-6033"),
        settings,
    )
    assert result.verdict is Verdict.NEEDS_HUMAN
    assert "not being auto-closed" in result.reasoning


async def test_unparseable_challenger_output_is_not_agreement(settings):
    ticket = load_ticket("TCK-6033")
    client = StubClient(["I think it's probably fine, honestly."] * 3, name="waffle")
    result = await challenge_resolution(client, ticket, _resolution("TCK-6033"), settings)
    assert result.verdict is Verdict.NEEDS_HUMAN


async def test_an_overturn_it_cannot_quote_collapses_to_needs_human(settings):
    """Suspicion is not an overturn — but it is still a reason not to close."""
    ticket = load_ticket("TCK-6033")
    client = StubClient(
        [
            json.dumps(
                {
                    "verdict": "overturned",
                    "reasoning": "This smells like a bug to me.",
                    "statement": "Something is wrong with reset tokens.",
                    "event_ids": [],
                    "file_quotes": [],
                }
            )
        ],
        name="hunch",
    )
    result = await challenge_resolution(client, ticket, _resolution("TCK-6033"), settings)
    assert result.verdict is Verdict.NEEDS_HUMAN
    assert "could not prove it" in result.reasoning


async def test_an_overturn_built_on_invented_citations_collapses_to_needs_human(settings):
    ticket = load_ticket("TCK-6033")
    client = StubClient(
        [
            json.dumps(
                {
                    "verdict": "overturned",
                    "reasoning": "The TTL check is inverted.",
                    "statement": "Inverted TTL comparison.",
                    "event_ids": ["auth-svc-2026-08-16#9999"],
                    "file_quotes": [
                        {"path": "src/auth/reset.ts", "quote": "if (age < 0) throw new Error();"}
                    ],
                }
            )
        ],
        name="fabricator",
    )
    result = await challenge_resolution(client, ticket, _resolution("TCK-6033"), settings)
    assert result.verdict is Verdict.NEEDS_HUMAN
    assert "did not resolve" in result.reasoning


async def test_a_provable_overturn_is_accepted_and_carries_its_chain(settings):
    ticket = load_ticket("TCK-6033")
    client = StubClient(
        [
            json.dumps(
                {
                    "verdict": "overturned",
                    "reasoning": "Tokens expire in seconds, not the documented hour.",
                    "statement": "verifyResetToken compares milliseconds against seconds.",
                    "event_ids": ["auth-svc-2026-08-16#2", "auth-svc-2026-08-16#4"],
                    "file_quotes": [
                        {
                            "path": "src/auth/reset.ts",
                            "quote": 'logger.warn("reset token expired", {',
                        }
                    ],
                }
            )
        ],
        name="gemini-stub",
    )
    result = await challenge_resolution(client, ticket, _resolution("TCK-6033"), settings)
    assert result.verdict is Verdict.OVERTURNED
    assert result.chain is not None
    assert result.chain.is_linked
    assert result.chain.code_spans[0].path == "src/auth/reset.ts"


async def test_upholding_a_correct_resolution_is_a_success(settings):
    """An export rejected for exceeding a published limit is the product working."""
    ticket = load_ticket("TCK-5210")
    client = StubClient(
        [
            json.dumps(
                {
                    "verdict": "upheld",
                    "reasoning": "320 MB against a documented 50 MB ceiling. Nothing is broken.",
                }
            )
        ],
        name="gemini-stub",
    )
    result = await challenge_resolution(client, ticket, _resolution("TCK-5210"), settings)
    assert result.verdict is Verdict.UPHELD
    assert result.chain is None


async def test_the_challenger_sees_a_wider_window_than_the_investigator(settings):
    """Rule 1: re-reading the same evidence and agreeing is not verification."""
    ticket = load_ticket("TCK-6033")
    client = StubClient([json.dumps({"verdict": "upheld", "reasoning": "fine"})], name="spy")
    await challenge_resolution(client, ticket, _resolution("TCK-6033"), settings)

    _, prompt = client.calls[0]
    assert f"±{settings.log_window_hours * 4}h" in prompt
    assert "usr_2287" in prompt, "accounts other than the reporter's must be visible"


async def test_a_ticket_with_no_matching_logs_gets_no_borrowed_evidence(settings):
    """The regression that matters: an unrelated incident must not be offered up
    as material for a blank-dashboard ticket."""
    ticket = load_ticket("TCK-7714")
    client = StubClient([json.dumps({"verdict": "upheld", "reasoning": "fine"})], name="spy")
    await challenge_resolution(client, ticket, _resolution("TCK-7714"), settings)

    _, prompt = client.calls[0]
    assert "None recorded" in prompt
    assert "RESET_TOKEN_EXPIRED" not in prompt
    assert "UPSTREAM_RETRY" not in prompt


@pytest.mark.parametrize("verdict", ["upheld", "needs_human"])
async def test_non_overturn_verdicts_pass_through_without_a_chain(settings, verdict):
    ticket = load_ticket("TCK-1102")
    client = StubClient([json.dumps({"verdict": verdict, "reasoning": "r"})], name="s")
    result = await challenge_resolution(client, ticket, _resolution("TCK-1102"), settings)
    assert result.verdict is Verdict(verdict)
    assert result.chain is None
