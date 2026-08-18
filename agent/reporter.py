"""Scoring, tracker payloads, and rendering. No LLM calls live in this module.

Everything here is deterministic on purpose. Risk scores that a model invents
cannot be compared across tickets, and a Jira description assembled by a model is
a description that can quietly omit the evidence. Both are built from the
verified chain instead, so the report says exactly what was proven and nothing
more.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agent.config import Settings
from agent.patcher import describe_patch
from agent.schemas import (
    SEVERITY_WEIGHT,
    Diagnosis,
    Disposition,
    JiraIssue,
    Patch,
    Severity,
    Ticket,
    TriageReport,
    Verdict,
)

PRIORITY_BY_SEVERITY: dict[Severity, str] = {
    Severity.CRITICAL: "Highest",
    Severity.HIGH: "High",
    Severity.MEDIUM: "Medium",
    Severity.LOW: "Low",
}

COMPONENT_BY_PREFIX: dict[str, str] = {
    "src/auth": "authentication",
    "src/billing": "billing",
    "src/api": "public-api",
    "src/exports": "exports",
    "src/platform": "platform",
}


# --- Scoring -----------------------------------------------------------------


def risk_score(diagnosis: Diagnosis, disposition: Disposition) -> float:
    """A comparable 0–100 number for queue ordering.

    Severity contributes up to 80 and blast radius up to 20, so a medium-severity
    defect hitting a dozen accounts outranks a high-severity one that has been
    seen once. Anything not being escalated scores a flat 0 — a resolved ticket
    and a defect are not on the same scale, and putting them there would let a
    well-answered how-to outrank a real fault.
    """
    if disposition is not Disposition.ESCALATE:
        return 0.0

    severity_component = SEVERITY_WEIGHT[diagnosis.severity] * 8.0
    blast = diagnosis.chain.blast_radius if diagnosis.chain else 0
    blast_component = min(blast, 5) * 4.0
    return round(min(100.0, severity_component + blast_component), 1)


def component_for(paths: list[str]) -> list[str]:
    """Map cited files to tracker components."""
    found: dict[str, None] = {}
    for path in paths:
        for prefix, component in COMPONENT_BY_PREFIX.items():
            if path.startswith(prefix):
                found.setdefault(component, None)
    return list(found)


# --- Tracker payload ---------------------------------------------------------


def build_jira_issue(
    ticket: Ticket,
    diagnosis: Diagnosis,
    patch: Patch | None,
    settings: Settings,
) -> JiraIssue:
    """Assemble the escalation. Every claim in it is one that survived checking."""
    chain = diagnosis.chain
    paths = sorted({s.path for s in chain.code_spans}) if chain else []
    score = risk_score(diagnosis, Disposition.ESCALATE)

    labels = ["autonomous-triage", f"intent-{diagnosis.intent.value}"]
    if score >= settings.page_threshold:
        labels.append("page-oncall")
    if diagnosis.fabrications:
        labels.append("unverified-claims-present")
    if patch is not None and patch.applies:
        labels.append("has-candidate-patch")

    return JiraIssue(
        project=settings.jira_project,
        issue_type="Bug",
        summary=_summary_line(ticket, diagnosis),
        description=_description(ticket, diagnosis, patch, score),
        priority=PRIORITY_BY_SEVERITY[diagnosis.severity],
        labels=labels,
        source_ticket=ticket.ticket_id,
        components=component_for(paths),
    )


def _summary_line(ticket: Ticket, diagnosis: Diagnosis) -> str:
    primary = diagnosis.chain.code_spans[0].path if diagnosis.chain else "unlocated"
    head = f"[{ticket.ticket_id}] {ticket.subject} — {primary}"
    return head[:255]


def _description(
    ticket: Ticket,
    diagnosis: Diagnosis,
    patch: Patch | None,
    score: float,
) -> str:
    chain = diagnosis.chain
    lines: list[str] = [
        "h2. Root cause",
        diagnosis.summary,
        "",
        "h2. Reported symptom",
        f"{ticket.account} ({ticket.plan}) via {ticket.channel}, "
        f"{ticket.created_at:%Y-%m-%d %H:%M UTC}",
        f"bq. {' '.join(ticket.body.split())[:600]}",
        "",
    ]

    if chain is not None:
        lines += [
            "h2. Log evidence",
            *(f"* {{{{{e.locator}}}}} {e.raw}" for e in chain.log_events),
            "",
            "h2. Code evidence",
            *(f"* {{{{{s.locator}}}}}\n{{code}}\n{s.snippet}\n{{code}}" for s in chain.code_spans),
            "",
            "h2. Why these are connected",
            f"Shared diagnostic terms: {', '.join(chain.link_terms)} "
            f"(link strength {chain.link_strength:.2f}).",
            f"Distinct users affected in the cited window: {chain.blast_radius}.",
            "",
        ]
        if chain.commits:
            lines += [
                "h2. Related commits",
                *(
                    f"* {c.sha} {c.authored_at:%Y-%m-%d} {c.author} — {c.subject}"
                    + (f" (PR #{c.pull_request})" if c.pull_request else "")
                    for c in chain.commits
                ),
                "",
            ]
        lines += ["h2. Agent reasoning", chain.reasoning, ""]

    if patch is not None:
        lines += ["h2. Candidate patch", describe_patch(patch), ""]
        if patch.applies:
            lines += [
                "{code:diff}",
                patch.diff.rstrip(),
                "{code}",
                "",
                f"*Rationale:* {patch.rationale}",
                f"*Missing test:* {patch.test_hint}" if patch.test_hint else "",
                f"*Anchored to file sha:* {patch.anchor_sha}",
                "",
            ]
        else:
            lines += [f"The proposed fix was rejected: {patch.rejection_reason}", ""]

    if diagnosis.fabrications:
        lines += [
            "h2. Unverified claims (rejected)",
            "The investigating model asserted the following and they could not be "
            "found in the logs or the source tree. They are listed so this report "
            "can be judged on what survived checking:",
            *(f"* {f}" for f in diagnosis.fabrications),
            "",
        ]

    lines += [
        "h2. Triage metadata",
        f"* Risk score: {score:.1f}/100",
        f"* Severity: {diagnosis.severity.value}",
        f"* Classifier confidence: {diagnosis.confidence:.2f}",
        f"* Investigating model: {diagnosis.agent_name}",
        "",
        "_Raised automatically. Every quote above was verified against the file or "
        "log line it names before this issue was created._",
    ]

    return "\n".join(line for line in lines if line != "")


# --- Rendering ---------------------------------------------------------------


def to_markdown(report: TriageReport) -> str:
    """The full report as markdown, for pasting into a review or a PR."""
    d = report.diagnosis
    lines = [
        f"# {report.ticket_id} — {report.final_disposition.value}",
        "",
        f"- **Intent:** {d.intent.value} (confidence {d.confidence:.2f})",
        f"- **Severity:** {d.severity.value}",
        f"- **Risk score:** {report.risk_score:.1f}/100",
        f"- **Classifier:** {report.classification.rationale}",
        "",
        "## Diagnosis",
        "",
        d.summary,
        "",
    ]

    if d.chain is not None:
        lines += [
            "## Verified evidence",
            "",
            f"Shared terms: `{'`, `'.join(d.chain.link_terms) or '—'}` "
            f"(strength {d.chain.link_strength:.2f}), "
            f"{d.chain.blast_radius} distinct user(s) affected.",
            "",
            "### Logs",
            "",
            *(f"- `{e.locator}` {e.raw}" for e in d.chain.log_events),
            "",
            "### Code",
            "",
        ]
        for span in d.chain.code_spans:
            lines += [f"`{span.locator}`", "", f"```{span.language}", span.snippet, "```", ""]

    if report.challenge is not None:
        c = report.challenge
        lines += [
            "## Challenge",
            "",
            f"**{c.verdict.value}** by `{c.challenger_model}` — {c.reasoning}",
            "",
        ]

    if report.patch is not None:
        lines += ["## Candidate patch", "", describe_patch(report.patch), ""]
        if report.patch.applies:
            lines += ["```diff", report.patch.diff.rstrip(), "```", ""]
            if report.patch.test_hint:
                lines += [f"**Missing test:** {report.patch.test_hint}", ""]

    if d.fabrications:
        lines += [
            "## Unverified claims (rejected)",
            "",
            *(f"- {f}" for f in d.fabrications),
            "",
        ]

    if report.receipt is not None:
        lines += [f"## Escalated as `{report.receipt.issue_key}`", ""]

    if report.customer_reply:
        lines += ["## Customer reply", "", report.customer_reply, ""]

    if report.trace:
        lines += [
            "## Tool trace",
            "",
            "| # | Tool | Arguments | Results |",
            "| --- | --- | --- | --- |",
        ]
        for step in report.trace:
            args = ", ".join(f"{k}={v}" for k, v in step.arguments.items())
            lines.append(f"| {step.index} | `{step.tool.value}` | {args} | {step.result_count} |")
        lines.append("")

    if report.model_notes:
        lines += ["## Notes", "", *(f"- {n}" for n in report.model_notes), ""]

    return "\n".join(lines)


def save_report(report: TriageReport, out_dir: Path) -> Path:
    """Write the report as JSON. Returns the path written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{report.ticket_id}-{stamp}.json"
    path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def queue_summary(reports: list[TriageReport]) -> dict[str, int]:
    """Counts for a run over more than one ticket."""
    return {
        "tickets": len(reports),
        "escalated": sum(1 for r in reports if r.final_disposition is Disposition.ESCALATE),
        "resolved": sum(1 for r in reports if r.final_disposition is Disposition.RESOLVE),
        "needs_human": sum(1 for r in reports if r.final_disposition is Disposition.NEEDS_HUMAN),
        "overturned": sum(1 for r in reports if r.was_overturned),
        "challenges": sum(1 for r in reports if r.challenge is not None),
        "upheld": sum(
            1
            for r in reports
            if r.challenge is not None and r.challenge.verdict is Verdict.UPHELD
        ),
        "patches_applied": sum(1 for r in reports if r.patch is not None and r.patch.applies),
        "fabrications": sum(len(r.diagnosis.fabrications) for r in reports),
    }
