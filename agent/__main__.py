"""Command line interface.

Four of the five commands need no API key: `tickets`, `inspect`, `tools`, and
`run --simulate`. That is deliberate — a reviewer should be able to clone this
repository and watch the evidence gate work before deciding whether to spend a
token on it.
"""

from __future__ import annotations

import asyncio
import json
import sys

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from agent import code_tools
from agent.config import get_settings
from agent.llm import ModelError, build_client
from agent.patcher import describe_patch
from agent.reporter import queue_summary, save_report, to_markdown
from agent.sandbox import all_tickets, load_ticket
from agent.schemas import Disposition, ToolName, Verdict
from agent.tools import kb, logs
from agent.tools.tracker import build_sink
from agent.triage_agent import AgentClients, triage, triage_many

# A legacy Windows console reports cp1252, which cannot encode an arrow or a
# box-drawing character, and Python raises UnicodeEncodeError mid-render rather
# than printing anything. Degrading those characters to "?" is strictly better
# than a traceback in place of a triage report. `chcp 65001` avoids it entirely.
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

app = typer.Typer(
    add_completion=False,
    help="Autonomous L1 support agent. Investigates tickets against real logs and source.",
)
console = Console()

DISPOSITION_STYLE = {
    Disposition.ESCALATE: "bold red",
    Disposition.RESOLVE: "green",
    Disposition.NEEDS_HUMAN: "yellow",
}


def _clients(simulate: bool, *, signal_aware: bool = True) -> AgentClients:
    if simulate:
        from agent.evals.simulated import simulated_clients

        return simulated_clients(signal_aware=signal_aware)

    settings = get_settings()
    try:
        challenger = build_client(settings, "challenger")
    except ModelError as exc:
        console.print(f"[yellow]No challenger available:[/yellow] {exc}")
        console.print(
            "[yellow]Every auto-close will be routed to a human instead of being "
            "closed unchallenged.[/yellow]"
        )
        challenger = None

    return AgentClients(
        classifier=build_client(settings, "classifier"),
        investigator=build_client(settings, "investigator"),
        patcher=build_client(settings, "patcher"),
        challenger=challenger,
    )


# --- Commands ----------------------------------------------------------------


@app.command()
def tickets() -> None:
    """List the inbound queue. No API key needed."""
    table = Table(title="Inbound queue", show_lines=False)
    table.add_column("Ticket")
    table.add_column("Opened")
    table.add_column("Account")
    table.add_column("Subject")
    for ticket in all_tickets():
        table.add_row(
            ticket.ticket_id,
            f"{ticket.created_at:%Y-%m-%d %H:%M}",
            ticket.account or "—",
            ticket.subject,
        )
    console.print(table)


@app.command()
def inspect(ticket_id: str) -> None:
    """Show a ticket with the logs and help articles around it. No API key needed."""
    ticket = load_ticket(ticket_id)
    settings = get_settings()

    console.print(Panel(ticket.body, title=f"{ticket.ticket_id} — {ticket.subject}"))

    events = logs.search_error_logs(
        user_id=ticket.user_id,
        around=ticket.created_at,
        window_hours=settings.log_window_hours,
    )
    if events:
        table = Table(title=f"Failures for {ticket.user_id} (±{settings.log_window_hours}h)")
        table.add_column("Event")
        table.add_column("Level")
        table.add_column("Code")
        table.add_column("Message")
        for event in events:
            table.add_row(event.event_id, event.level.value, event.code or "—", event.message)
        console.print(table)
    else:
        console.print(
            f"[yellow]No failures logged for {ticket.user_id} in the window.[/yellow] "
            "Absence is evidence too — it usually means the fault is client-side."
        )

    hits = kb.search_docs(f"{ticket.subject} {ticket.body}")
    if hits:
        table = Table(title="Help-centre matches")
        table.add_column("Article")
        table.add_column("Score", justify="right")
        table.add_column("Title")
        for hit in hits:
            table.add_row(hit.article_id, f"{hit.score:.1f}", hit.title)
        console.print(table)


@app.command()
def tools() -> None:
    """Show the agent's action space and the estate it acts on. No API key needed."""
    table = Table(title="Tools")
    table.add_column("Tool")
    table.add_column("Module")
    table.add_column("LLM?")
    for name, module in [
        (ToolName.SEARCH_ERROR_LOGS, "agent.tools.logs"),
        (ToolName.READ_LOG_WINDOW, "agent.tools.logs"),
        (ToolName.SEARCH_CODEBASE, "agent.code_tools"),
        (ToolName.READ_FILE, "agent.code_tools"),
        (ToolName.GIT_LOG, "agent.code_tools"),
        (ToolName.SEARCH_DOCS, "agent.tools.kb"),
    ]:
        table.add_row(name.value, module, "no")
    console.print(table)

    console.print(
        f"\nSandbox: [bold]{len(code_tools.list_files())}[/bold] source files, "
        f"[bold]{len(logs.all_events())}[/bold] log events, "
        f"[bold]{len(kb.all_articles())}[/bold] help articles, "
        f"[bold]{len(all_tickets())}[/bold] tickets."
    )
    console.print(
        "\nGates: [bold]cite_code[/bold] and [bold]cite_log[/bold] verify every quote; "
        "[bold]evidence.build_chain[/bold] refuses to link code that shares no terms "
        "with the error; [bold]patcher.verify_patch[/bold] rejects a fix whose anchor "
        "is not in the file."
    )


@app.command()
def run(
    ticket_id: str = typer.Argument(None, help="Ticket to triage. Omit to run the whole queue."),
    simulate: bool = typer.Option(False, "--simulate", help="Use deterministic stand-ins."),
    naive: bool = typer.Option(False, "--naive", help="Simulate with the keyword-only classifier."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show the tool trace."),
    markdown: bool = typer.Option(False, "--markdown", help="Print the full markdown report."),
    save: bool = typer.Option(False, "--save", help="Write the report JSON to reports/."),
    live: bool = typer.Option(False, "--live", help="Create real tracker issues. Needs a token."),
) -> None:
    """Triage one ticket, or the whole queue."""
    settings = get_settings()
    clients = _clients(simulate, signal_aware=not naive)
    sink = build_sink(settings, live=live)

    if ticket_id:
        report = asyncio.run(triage(load_ticket(ticket_id), clients, settings=settings, sink=sink))
        _render(report, verbose=verbose, markdown=markdown)
        if save:
            console.print(f"\nSaved to {save_report(report, settings.report_dir)}")
        return

    reports = asyncio.run(
        triage_many(list(all_tickets()), clients, settings=settings, sink=sink)
    )
    _render_queue(reports)
    if save:
        for report in reports:
            save_report(report, settings.report_dir)
        console.print(f"\nSaved {len(reports)} reports to {settings.report_dir}")


@app.command()
def evaluate(
    json_out: bool = typer.Option(False, "--json", help="Emit raw results as JSON."),
) -> None:
    """Run the golden-ticket eval. No API key needed."""
    from agent.evals.run import main as run_eval

    results = run_eval(render=not json_out)
    if json_out:
        console.print_json(json.dumps(results))


# --- Rendering ---------------------------------------------------------------


def _render(report, *, verbose: bool, markdown: bool) -> None:
    if markdown:
        console.print(to_markdown(report))
        return

    d = report.diagnosis
    style = DISPOSITION_STYLE[report.final_disposition]
    console.print(
        Panel(
            f"[{style}]{report.final_disposition.value.upper()}[/{style}]  "
            f"intent={d.intent.value}  severity={d.severity.value}  "
            f"risk={report.risk_score:.0f}/100",
            title=f"{report.ticket_id}",
        )
    )

    console.print(f"\n[bold]Diagnosis[/bold]\n{d.summary}")

    if d.chain is not None:
        console.print(
            f"\n[bold]Verified evidence[/bold] — shared terms: "
            f"[cyan]{', '.join(d.chain.link_terms) or 'none'}[/cyan] "
            f"(strength {d.chain.link_strength:.2f}), "
            f"{d.chain.blast_radius} user(s) affected"
        )
        for event in d.chain.log_events[:6]:
            console.print(f"  [dim]{event.locator}[/dim] {event.raw[:140]}")
        for span in d.chain.code_spans:
            console.print(f"\n  [dim]{span.locator}[/dim]")
            console.print(Syntax(span.snippet, span.language, background_color="default"))
    else:
        console.print("\n[yellow]No verified evidence chain.[/yellow]")

    if d.fabrications:
        console.print("\n[bold red]Rejected claims[/bold red]")
        for item in d.fabrications:
            console.print(f"  [red]✗[/red] {item}")

    if report.challenge is not None:
        c = report.challenge
        colour = {
            Verdict.OVERTURNED: "bold red",
            Verdict.UPHELD: "green",
            Verdict.NEEDS_HUMAN: "yellow",
        }[c.verdict]
        console.print(
            f"\n[bold]Challenge[/bold] ([dim]{c.challenger_model}[/dim]): "
            f"[{colour}]{c.verdict.value}[/{colour}] — {c.reasoning}"
        )

    if report.patch is not None:
        console.print(f"\n[bold]Candidate patch[/bold] {describe_patch(report.patch)}")
        if report.patch.applies:
            console.print(Syntax(report.patch.diff, "diff", background_color="default"))

    if report.receipt is not None:
        console.print(f"\n[bold]Escalated[/bold] as {report.receipt.issue_key}")

    if report.customer_reply:
        console.print(Panel(report.customer_reply, title="Customer reply"))

    if report.model_notes:
        console.print("\n[bold]Notes[/bold]")
        for note in report.model_notes:
            console.print(f"  • {note}")

    if verbose and report.trace:
        table = Table(title="Tool trace")
        table.add_column("#", justify="right")
        table.add_column("Tool")
        table.add_column("Arguments")
        table.add_column("Results", justify="right")
        for step in report.trace:
            args = ", ".join(f"{k}={v}" for k, v in step.arguments.items())
            table.add_row(str(step.index), step.tool.value, args, str(step.result_count))
        console.print(table)


def _render_queue(reports) -> None:
    table = Table(title="Triage run")
    table.add_column("Ticket")
    table.add_column("Intent")
    table.add_column("Decision")
    table.add_column("Risk", justify="right")
    table.add_column("Cited file")
    table.add_column("Challenge")

    for report in sorted(reports, key=lambda r: -r.risk_score):
        style = DISPOSITION_STYLE[report.final_disposition]
        chain = report.diagnosis.chain
        cited = chain.code_spans[0].path if chain and chain.code_spans else "—"
        challenge = report.challenge.verdict.value if report.challenge else "—"
        table.add_row(
            report.ticket_id,
            report.diagnosis.intent.value,
            f"[{style}]{report.final_disposition.value}[/{style}]",
            f"{report.risk_score:.0f}" if report.risk_score else "—",
            cited,
            challenge,
        )
    console.print(table)

    summary = queue_summary(reports)
    console.print(
        f"\n{summary['tickets']} tickets — "
        f"[red]{summary['escalated']} escalated[/red], "
        f"[green]{summary['resolved']} resolved[/green], "
        f"[yellow]{summary['needs_human']} to a human[/yellow]. "
        f"{summary['challenges']} challenge(s): {summary['overturned']} overturned, "
        f"{summary['upheld']} upheld. "
        f"{summary['patches_applied']} patch(es) applied, "
        f"{summary['fabrications']} fabricated citation(s) rejected."
    )


if __name__ == "__main__":  # pragma: no cover
    app()
