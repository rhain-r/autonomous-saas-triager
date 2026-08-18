"""Shared fixtures. No test in this suite makes a network call."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agent.config import Settings
from agent.llm import StubClient
from agent.sandbox import load_ticket
from agent.schemas import FileQuote, Hypothesis, Ticket
from agent.tools.tracker import FileSink
from agent.triage_agent import AgentClients


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings with all writes redirected into the test's tmp_path."""
    return Settings(
        anthropic_api_key="test-key",
        google_api_key="test-key",
        report_dir=tmp_path / "reports",
        trace_dir=tmp_path / "runs",
    )


@pytest.fixture
def sink(tmp_path) -> FileSink:
    return FileSink(tmp_path / "escalations")


@pytest.fixture
def ticket() -> Ticket:
    """TCK-3021: the OAuth redirect loop. The clean end-to-end case."""
    return load_ticket("TCK-3021")


@pytest.fixture
def quiet_ticket() -> Ticket:
    """TCK-7714: a blank dashboard, with no server-side evidence at all."""
    return load_ticket("TCK-7714")


@pytest.fixture
def real_hypothesis() -> Hypothesis:
    """A hypothesis whose every citation resolves against the sandbox."""
    return Hypothesis(
        statement="redirectUri is built from the staging origin.",
        event_ids=["auth-svc-2026-08-14#3", "auth-svc-2026-08-14#5"],
        file_quotes=[
            FileQuote(
                path="src/auth/config.ts",
                quote="redirectUri: `${STAGING_ORIGIN}/auth/callback`,",
            )
        ],
        reasoning="Okta rejects the exchange because the supplied URI is the staging one.",
    )


@pytest.fixture
def fabricated_hypothesis() -> Hypothesis:
    """Every citation in this one is invented. Nothing should survive."""
    return Hypothesis(
        statement="The session cache is misconfigured.",
        event_ids=["auth-svc-2026-08-14#9999", "does-not-exist#1"],
        file_quotes=[
            FileQuote(path="src/auth/config.ts", quote="const sessionCache = new LruCache(512);"),
            FileQuote(path="src/auth/nonexistent.ts", quote="anything at all"),
        ],
        reasoning="Plausible, entirely unsupported.",
    )


def stub_bundle(
    classification: dict,
    hypothesis: dict,
    *,
    patch: dict | None = None,
    challenge: dict | None = None,
    reply: str = "A reply.",
) -> AgentClients:
    """Assemble canned clients for one full `triage()` call.

    The investigator client answers twice — once for the hypothesis, once for
    the customer reply — because the real pipeline reuses it for both.
    """
    responses = [json.dumps(hypothesis), reply]
    return AgentClients(
        classifier=StubClient([json.dumps(classification)], name="stub-classifier"),
        investigator=StubClient(responses, name="stub-investigator"),
        patcher=StubClient([json.dumps(patch)] if patch else [], name="stub-patcher"),
        challenger=(
            StubClient([json.dumps(challenge)], name="stub-challenger") if challenge else None
        ),
    )


def at(hour: int, minute: int = 0, day: int = 14) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)
