"""The simulated production estate the agent investigates.

Four things live under this package, and all four are real files rather than
fixtures generated at import time:

- `repo/`     a small TypeScript service tree, with genuine defects planted in it
- `logs/`     service logs in the format `repo/src/platform/logger.ts` emits
- `kb/`       help-centre articles, including the ones that are tempting and wrong
- `tickets/`  the inbound queue

Keeping them on disk is the point. `search_codebase()` greps a real tree,
`cite_code()` verifies against real bytes, and the line numbers in a report are
line numbers you can open. Swapping this package for a real repository, a real
log store, and a real helpdesk is a change to `agent/config.py` paths and the
tool implementations behind them — not to any agent.
"""

from __future__ import annotations

from functools import lru_cache

from agent.config import SANDBOX_TICKETS
from agent.schemas import Ticket


@lru_cache(maxsize=1)
def all_tickets() -> tuple[Ticket, ...]:
    """Every ticket in the inbox, oldest first."""
    tickets = [
        Ticket.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(SANDBOX_TICKETS.glob("*.json"))
    ]
    tickets.sort(key=lambda t: t.created_at)
    return tuple(tickets)


def load_ticket(ticket_id: str) -> Ticket:
    """Fetch one ticket by id, case-insensitively."""
    wanted = ticket_id.strip().upper()
    for ticket in all_tickets():
        if ticket.ticket_id.upper() == wanted:
            return ticket
    known = ", ".join(t.ticket_id for t in all_tickets())
    raise KeyError(f"unknown ticket {ticket_id!r}. Available: {known}")


def ticket_ids() -> list[str]:
    return [t.ticket_id for t in all_tickets()]


def clear_cache() -> None:
    """Drop the ticket cache. Call after writing a ticket file in a test."""
    all_tickets.cache_clear()
