"""Run every golden ticket through four configurations and report.

The configurations ablate the two things worth arguing about:

- **classifier quality** — naive keyword matching versus a stand-in that also
  notices a customer quoting the product's own promise back at it;
- **the challenger** — on or off.

Four runs, no API keys, no network, about a second. `uv run python -m
agent.evals.run`.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from agent.config import AGENT_DIR, get_settings
from agent.evals.scoring import Metrics, load_keys, score_report
from agent.evals.simulated import simulated_clients
from agent.sandbox import all_tickets
from agent.tools.tracker import FileSink
from agent.triage_agent import triage_many

RESULTS_DIR = AGENT_DIR / "evals" / "results"

# The "unguarded" configurations close a ticket on the classifier's word alone,
# which is what an ordinary LLM triage bot does. They are the baseline the
# challenger has to beat, and the only fair one: a system that simply refuses to
# close anything also has zero silent closures, and is useless.
CONFIGURATIONS = [
    ("keyword-only classifier, unguarded close", False, False),
    ("keyword-only classifier, + challenger", False, True),
    ("signal-aware classifier, unguarded close", True, False),
    ("signal-aware classifier, + challenger", True, True),
]

console = Console()


def run_configuration(
    label: str,
    signal_aware: bool,
    with_challenger: bool,
    *,
    sink_dir: Path | None = None,
) -> Metrics:
    settings = get_settings().model_copy(update={"require_challenge": with_challenger})
    clients = simulated_clients(signal_aware=signal_aware, with_challenger=with_challenger)
    # Escalations go to a scratch sink: an eval run must not leave issue payloads
    # in the directory a real run writes to.
    sink = FileSink(sink_dir or RESULTS_DIR / "_sink")
    reports = asyncio.run(
        triage_many(list(all_tickets()), clients, settings=settings, sink=sink)
    )
    return Metrics(label=label, outcomes=[score_report(r) for r in reports])


def main(render: bool = True) -> dict[str, object]:
    keys = load_keys()
    results = [run_configuration(*config) for config in CONFIGURATIONS]

    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tickets": len(keys),
        "note": (
            "Produced with deterministic lexical stand-ins, not language models. "
            "These numbers measure pipeline behaviour — the evidence gate, the "
            "challenge routing, the patch gate — and say nothing about model "
            "accuracy. No API key was used."
        ),
        "configurations": [m.as_dict() for m in results],
        "per_ticket": [
            {
                "configuration": m.label,
                "outcomes": [
                    {
                        "ticket_id": o.ticket_id,
                        "trap": o.trap,
                        "expected": o.expected.value,
                        "actual": o.actual.value,
                        "expected_intent": o.expected_intent.value,
                        "actual_intent": o.actual_intent.value,
                        "cited_paths": o.cited_paths,
                        "attribution": o.attribution,
                        "overturned": o.overturned,
                        "silent_closure": o.silent_closure,
                    }
                    for o in m.outcomes
                ],
            }
            for m in results
        ],
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    if render:
        _render(results, out)
    return payload


def _render(results: list[Metrics], out: Path) -> None:
    table = Table(title="Escalation decisions vs golden keys")
    table.add_column("Configuration")
    table.add_column("P", justify="right")
    table.add_column("R", justify="right")
    table.add_column("F1", justify="right")
    table.add_column("FP", justify="right")
    table.add_column("FN", justify="right")
    table.add_column("Silent\nclosures", justify="right")
    table.add_column("Held for\nhuman", justify="right")
    table.add_column("Right\nfile", justify="right")

    for m in results:
        silent = (
            f"[bold red]{m.silent_closures}[/bold red]"
            if m.silent_closures
            else "[green]0[/green]"
        )
        table.add_row(
            m.label,
            f"{m.precision:.3f}",
            f"{m.recall:.3f}",
            f"{m.f1:.3f}",
            str(m.false_positives),
            str(m.false_negatives),
            silent,
            str(m.contained),
            f"{m.attribution_correct}/{m.attribution_scored}",
        )
    console.print(table)

    detail = Table(title="Per-ticket, signal-aware classifier + challenger")
    detail.add_column("Ticket")
    detail.add_column("Trap")
    detail.add_column("Expected")
    detail.add_column("Actual")
    detail.add_column("Cited file")
    detail.add_column("Attribution")

    best = results[-1]
    for o in sorted(best.outcomes, key=lambda x: x.ticket_id):
        ok = "green" if o.disposition_correct else "red"
        detail.add_row(
            o.ticket_id,
            o.trap or "—",
            o.expected.value,
            f"[{ok}]{o.actual.value}[/{ok}]",
            ", ".join(o.cited_paths) or "—",
            o.attribution,
        )
    console.print(detail)

    overturns = sum(m.overturns for m in results)
    correct = sum(m.correct_overturns for m in results)
    console.print(
        f"\n[bold]{overturns} overturn(s) across all configurations, "
        f"{correct} of them on genuine defects.[/bold] "
        f"{sum(m.fabrications for m in results)} fabricated citation(s) rejected."
    )
    console.print(
        "\n[dim]Deterministic stand-ins, not models. This measures the pipeline — "
        "evidence gate, challenge routing, patch gate — not model accuracy. "
        "No API key was used.[/dim]"
    )
    console.print(f"[dim]Raw results: {out}[/dim]")


if __name__ == "__main__":  # pragma: no cover
    main()
