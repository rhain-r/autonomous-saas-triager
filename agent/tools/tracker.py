"""Issue-tracker sink. No LLM calls live in this module.

Escalation is the one thing this system does that reaches outside itself, so it
goes through a named seam rather than a `requests.post` buried in an agent.

`FileSink` is the default and writes the payload to `runs/`. `JiraSink` is the
real thing and is never selected implicitly — creating tickets in someone's live
project as a side effect of running a demo is not a default anybody wants.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agent.config import Settings
from agent.schemas import JiraIssue, TrackerReceipt


class TrackerSink(Protocol):
    """Where a verified escalation goes."""

    name: str

    def create(self, issue: JiraIssue) -> TrackerReceipt: ...


class FileSink:
    """Writes the issue payload to disk and returns a synthetic key.

    The payload is the exact JSON a Jira REST call would carry, so the only
    difference between this and production is the transport.
    """

    name = "file"

    def __init__(self, out_dir: Path, base_url: str = "https://tracker.local") -> None:
        self._dir = out_dir
        self._base_url = base_url.rstrip("/")

    def create(self, issue: JiraIssue) -> TrackerReceipt:
        self._dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        key = f"{issue.project}-{abs(hash(issue.source_ticket)) % 9000 + 1000}"
        path = self._dir / f"{key}-{stamp}.json"
        path.write_text(
            json.dumps(
                {"issue_key": key, "fields": issue.model_dump(mode="json")},
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return TrackerReceipt(issue_key=key, url=path.resolve().as_uri(), sink=self.name)


class JiraSink:
    """Real Jira Cloud. Requires an API token and is opt-in only."""

    name = "jira"

    def __init__(self, base_url: str, api_token: str, project: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = api_token
        self._project = project

    def create(self, issue: JiraIssue) -> TrackerReceipt:  # pragma: no cover - network
        import httpx

        response = httpx.post(
            f"{self._base_url}/rest/api/3/issue",
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "fields": {
                    "project": {"key": issue.project},
                    "issuetype": {"name": issue.issue_type},
                    "summary": issue.summary,
                    "description": issue.description,
                    "labels": issue.labels,
                }
            },
            timeout=30.0,
        )
        response.raise_for_status()
        key = response.json()["key"]
        return TrackerReceipt(
            issue_key=key,
            url=f"{self._base_url}/browse/{key}",
            sink=self.name,
        )


def build_sink(settings: Settings, *, live: bool = False) -> TrackerSink:
    """Pick a sink. `live=True` is the only way to reach a real tracker."""
    if live:
        if not settings.jira_api_token:
            raise RuntimeError(
                "live escalation requested but JIRA_API_TOKEN is unset; refusing to "
                "silently drop the escalation on the floor"
            )
        return JiraSink(settings.jira_base_url, settings.jira_api_token, settings.jira_project)
    return FileSink(settings.trace_dir / "escalations", settings.jira_base_url)
