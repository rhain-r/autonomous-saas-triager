"""Log search. No LLM calls live in this module.

Parses the service logs under `agent/sandbox/logs` into `LogEvent`s and lets the
agent slice them by user, time window, level, and text. `cite_log()` is the gate
that stops a model from quoting a log line that was never written.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from agent.config import SANDBOX_LOGS
from agent.schemas import LogEvent, LogLevel, tokenize

# 2026-08-14T09:12:45.660Z ERROR auth-svc msg="..." code="..." user="..."
_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s+"
    r"(?P<level>DEBUG|INFO|WARN|ERROR)\s+"
    r"(?P<service>[\w.-]+)\s+"
    r"(?P<fields>.*)$"
)
_FIELD = re.compile(r'(?P<key>[A-Za-z_][\w]*)="(?P<value>[^"]*)"')


def _parse_timestamp(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def parse_line(raw: str, source_file: str, line_no: int) -> LogEvent | None:
    """Parse one log line. Returns None for anything that does not match.

    Unparseable lines are dropped rather than guessed at. A malformed line that
    became a half-populated event would be indistinguishable from a real one by
    the time it reached the evidence gate.
    """
    match = _LINE.match(raw.strip())
    if not match:
        return None

    fields = {m.group("key"): m.group("value") for m in _FIELD.finditer(match.group("fields"))}
    return LogEvent(
        event_id=f"{Path(source_file).stem}#{line_no}",
        timestamp=_parse_timestamp(match.group("ts")),
        level=LogLevel(match.group("level").lower()),
        service=match.group("service"),
        message=fields.get("msg", ""),
        code=fields.get("code"),
        user_id=fields.get("user"),
        fields={k: v for k, v in fields.items() if k not in {"msg", "code", "user"}},
        raw=raw.rstrip(),
        source_file=source_file,
        line_no=line_no,
    )


@lru_cache(maxsize=1)
def _load_all() -> tuple[LogEvent, ...]:
    """Every parseable event across every log file, sorted by time."""
    events: list[LogEvent] = []
    for path in sorted(SANDBOX_LOGS.glob("*.log")):
        for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            event = parse_line(raw, path.name, line_no)
            if event is not None:
                events.append(event)
    events.sort(key=lambda e: (e.timestamp, e.source_file, e.line_no))
    return tuple(events)


# --- Tools -------------------------------------------------------------------


def search_error_logs(
    *,
    user_id: str | None = None,
    around: datetime | None = None,
    window_hours: int = 6,
    level: LogLevel | None = None,
    query: str = "",
    failures_only: bool = True,
    limit: int = 40,
) -> list[LogEvent]:
    """The agent's primary observation tool.

    Defaults to failures only. An investigator handed every INFO line spends its
    context on healthy traffic, and the signal it needs is three lines long.
    """
    events = list(_load_all())

    if user_id:
        events = [e for e in events if e.user_id == user_id]
    if level is not None:
        events = [e for e in events if e.level is level]
    elif failures_only:
        events = [e for e in events if e.level.is_failure]
    if around is not None:
        span = timedelta(hours=window_hours)
        events = [e for e in events if abs(e.timestamp - around) <= span]
    if query:
        events = _filter_by_query(events, query)

    return events[:limit]


def _filter_by_query(events: list[LogEvent], query: str) -> list[LogEvent]:
    """Keep events whose *signature* meaningfully overlaps the query.

    Two decisions here, both learned the hard way:

    **Match the signature, not the raw line.** A raw line carries a timestamp,
    a service name, and a user id. Matching on those links a ticket to whatever
    else happened that afternoon.

    **One shared word is not a match.** A query built from a whole ticket body
    has dozens of terms, and a single incidental overlap — a customer writing
    "request a new one" against a log line reading "retrying upstream request"
    — is enough to drag an unrelated service into the evidence. Long queries
    therefore need at least two shared terms. Short, deliberate queries (an
    error code, a symbol name) still match on one.
    """
    terms = tokenize(query)
    if not terms:
        return events
    threshold = 2 if len(terms) >= 4 else 1
    return [e for e in events if len(terms & tokenize(e.signature)) >= threshold]


def read_log_window(
    source_file: str,
    line_no: int,
    *,
    before: int = 3,
    after: int = 3,
) -> list[LogEvent]:
    """Neighbouring lines around one event, including INFO.

    What happened just *before* an error is usually the half of the story the
    error line omits — the authorize call that preceded the rejected exchange,
    the first delivery of a webhook that then arrived twice.
    """
    same_file = [e for e in _load_all() if e.source_file == source_file]
    return [e for e in same_file if line_no - before <= e.line_no <= line_no + after]


def cite_log(event_id: str, quote: str = "") -> LogEvent | None:
    """Resolve an event id, optionally checking that `quote` is really in it.

    The counterpart to `cite_code()`. A model that reports an event id it never
    saw, or attributes text to a line that does not contain it, gets `None` back
    and the caller records a fabrication.
    """
    for event in _load_all():
        if event.event_id != event_id:
            continue
        if quote and quote.strip() not in event.raw:
            return None
        return event
    return None


def all_events() -> tuple[LogEvent, ...]:
    """Every parsed event. Used by the eval harness and the `inspect` command."""
    return _load_all()


def clear_cache() -> None:
    """Drop the parsed-log cache. Call after writing a log file in a test."""
    _load_all.cache_clear()
