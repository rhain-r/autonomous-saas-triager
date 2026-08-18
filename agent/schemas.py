"""Message contracts between the triage agents.

Everything that crosses an agent boundary is defined here and validated on the
way through. `extra="forbid"` means a model that invents a field gets a
validation error rather than a silently ignored hallucination.

The load-bearing rule lives in `Diagnosis`: a ticket may not be escalated to
engineering, and may not be closed as "not a bug", unless the agent can produce
an `EvidenceChain` that survives verification against the real log lines and the
real source file. Everything else here exists to make that rule enforceable.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Base for every contract: unknown fields are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Enumerations ------------------------------------------------------------


class Intent(str, Enum):
    """What the customer actually wants, independent of how they phrased it."""

    BUG = "bug"
    USER_ERROR = "user_error"
    HOW_TO = "how_to"
    FEATURE_REQUEST = "feature_request"


class Disposition(str, Enum):
    """What the agent proposes to *do*. This is the output that costs money."""

    ESCALATE = "escalate"
    RESOLVE = "resolve"
    NEEDS_HUMAN = "needs_human"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


SEVERITY_WEIGHT: dict[Severity, float] = {
    Severity.CRITICAL: 10.0,
    Severity.HIGH: 6.0,
    Severity.MEDIUM: 3.0,
    Severity.LOW: 1.0,
}


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"

    @property
    def is_failure(self) -> bool:
        return self in (LogLevel.WARN, LogLevel.ERROR)


class Verdict(str, Enum):
    UPHELD = "upheld"
    OVERTURNED = "overturned"
    NEEDS_HUMAN = "needs_human"


class ToolName(str, Enum):
    """The agent's action space. Recorded in the trace so a run is auditable."""

    SEARCH_ERROR_LOGS = "search_error_logs"
    READ_LOG_WINDOW = "read_log_window"
    SEARCH_CODEBASE = "search_codebase"
    READ_FILE = "read_file"
    GIT_LOG = "git_log"
    SEARCH_DOCS = "search_docs"


# --- Tokenisation ------------------------------------------------------------

_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Words that appear in every log line and every source file. Left in the token
# stream they would make any code span "link" to any log event, which would make
# the evidence gate decorative.
STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "not", "for", "with", "from", "this", "that", "was",
        "return", "const", "let", "var", "function", "async", "await", "export",
        "import", "class", "interface", "type", "string", "number", "boolean",
        "true", "false", "null", "undefined", "new", "public", "private", "void",
        "error", "err", "msg", "log", "logger", "info", "warn", "debug", "code",
        "user", "detail", "status", "data", "value", "key", "name", "url",
        "http", "https", "com", "org", "net", "meridian",
    }
)


def tokenize(text: str) -> set[str]:
    """Normalise text to a comparable token set.

    Identifier styles are folded together — `redirectUri`, `redirect_uri`, and
    `REDIRECT_URI` all reduce to `{"redirect", "uri"}`. Without this the linker
    would never connect a log field to the camelCase symbol that produced it,
    which is the most common shape real evidence takes.
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", text)
    parts = (p.lower() for p in _TOKEN_SPLIT.split(spaced) if p)
    return {p for p in parts if len(p) > 2 and p not in STOPWORDS}


# --- Inputs ------------------------------------------------------------------


class Ticket(StrictModel):
    """An inbound support ticket, exactly as the helpdesk hands it over."""

    ticket_id: str
    subject: str
    body: str
    user_id: str | None = None
    account: str | None = None
    plan: str = "team"
    channel: str = "email"
    created_at: datetime
    product_area: str | None = None

    def render(self) -> str:
        """The ticket as shown to a model."""
        head = f"Ticket {self.ticket_id} — {self.subject}"
        meta = f"account={self.account} plan={self.plan} user_id={self.user_id}"
        return f"{head}\n{meta}\nopened {self.created_at.isoformat()}\n\n{self.body}"


# --- Observations from tools -------------------------------------------------


class LogEvent(StrictModel):
    """One parsed log line, still carrying where it came from.

    `raw` is the verbatim line. `cite_log()` refuses to mint an event whose text
    does not appear in the file, which is what makes a quoted log line evidence
    rather than a plausible sentence.
    """

    event_id: str
    timestamp: datetime
    level: LogLevel
    service: str
    message: str
    code: str | None = None
    user_id: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)
    raw: str
    source_file: str
    line_no: int = Field(ge=1)

    @property
    def locator(self) -> str:
        return f"{self.source_file}:{self.line_no}"

    @property
    def signature(self) -> str:
        """The part of the event that identifies *what went wrong*."""
        return " ".join(filter(None, [self.code, self.message, self.fields.get("detail")]))


class CodeSpan(StrictModel):
    """A verbatim slice of a real file at real line numbers.

    Produced only by `cite_code()`. `content_sha` pins the file contents at read
    time so a patch built against this span can be rejected if the file moved
    underneath it.
    """

    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    snippet: str = Field(min_length=1)
    language: str = "typescript"
    content_sha: str

    @model_validator(mode="after")
    def _ordered_lines(self) -> CodeSpan:
        if self.line_end < self.line_start:
            raise ValueError("line_end must not precede line_start")
        return self

    @property
    def locator(self) -> str:
        if self.line_end == self.line_start:
            return f"{self.path}:{self.line_start}"
        return f"{self.path}:{self.line_start}-{self.line_end}"


class CodeMatch(StrictModel):
    """A single `search_codebase()` hit. Cheap; not yet evidence."""

    path: str
    line_no: int = Field(ge=1)
    line: str
    score: float = 0.0


class Commit(StrictModel):
    """An entry from the repository history."""

    sha: str
    authored_at: datetime
    author: str
    subject: str
    body: str = ""
    files: list[str] = Field(default_factory=list)
    pull_request: int | None = None


class KbHit(StrictModel):
    """A help-centre article matched by `search_docs()`."""

    article_id: str
    title: str
    path: str
    excerpt: str
    score: float = 0.0


# --- Evidence ----------------------------------------------------------------


class EvidenceChain(StrictModel):
    """The causal claim, reduced to things that can be checked.

    A chain has two joints and both are checked:

    **log → code.** `link_terms` holds the tokens the error signature and the
    cited source actually share. It is computed by `agent.evidence.build_chain()`
    and never supplied by a model — an agent can assert a connection, but it
    cannot assert the overlap that proves one.

    **ticket → log.** `ticket_anchor` records why these log events belong to
    *this* customer's complaint: they carry the reporting user's id, or they
    share vocabulary with what the customer wrote. Without it a chain can be
    perfectly self-consistent and still describe an unrelated incident — which
    is not a hypothetical, it is what this system did before the check existed.
    """

    symptom: str = Field(min_length=1)
    log_events: list[LogEvent] = Field(min_length=1)
    code_spans: list[CodeSpan] = Field(min_length=1)
    link_terms: list[str] = Field(default_factory=list)
    ticket_anchor: str = ""
    commits: list[Commit] = Field(default_factory=list)
    reasoning: str = Field(min_length=1)

    @property
    def is_linked(self) -> bool:
        """Both joints hold. Either one alone is not a causal chain."""
        return bool(self.link_terms) and bool(self.ticket_anchor)

    @property
    def link_strength(self) -> float:
        """Overlap size, saturating at four shared terms.

        Deliberately coarse. It ranks chains; it does not pretend to measure
        causation, which no lexical statistic can do.
        """
        return min(len(self.link_terms), 4) / 4.0

    @property
    def blast_radius(self) -> int:
        """Distinct users visible in the cited log events."""
        return len({e.user_id for e in self.log_events if e.user_id})

    def render(self) -> str:
        logs = "\n".join(f"  [{e.locator}] {e.raw}" for e in self.log_events)
        code = "\n".join(f"  [{s.locator}]\n{s.snippet}" for s in self.code_spans)
        return f"symptom: {self.symptom}\nlogs:\n{logs}\ncode:\n{code}"


# --- Agent outputs -----------------------------------------------------------


class Classification(StrictModel):
    """First pass: what kind of ticket is this, and how sure are we."""

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1)
    suspected_area: str | None = None
    search_terms: list[str] = Field(default_factory=list)


class FileQuote(StrictModel):
    """A model's claim that this exact text appears in this exact file."""

    path: str
    quote: str = Field(min_length=1)


class Hypothesis(StrictModel):
    """A model's proposed root cause, before any of it has been checked.

    Nothing here is trusted. `event_ids` and `file_quotes` are resolved against
    the real log lines and the real files; anything that does not resolve is
    dropped and recorded as a fabrication.
    """

    statement: str = Field(min_length=1)
    event_ids: list[str] = Field(default_factory=list)
    file_quotes: list[FileQuote] = Field(default_factory=list)
    reasoning: str = Field(min_length=1)


class Diagnosis(StrictModel):
    """The triage decision. This is the artefact the integrity rules protect."""

    diagnosis_id: str = Field(default_factory=lambda: f"dgn_{uuid4().hex[:10]}")
    ticket_id: str
    intent: Intent
    disposition: Disposition
    severity: Severity = Severity.MEDIUM
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    summary: str = Field(min_length=1)
    chain: EvidenceChain | None = None
    kb_hits: list[KbHit] = Field(default_factory=list)
    fabrications: list[str] = Field(default_factory=list)
    agent_name: str = "investigator"

    @model_validator(mode="after")
    def _enforce_evidentiary_rules(self) -> Diagnosis:
        """The core integrity rule of the system.

        Escalation spends engineering time, so it must be paid for with a linked
        chain. Claiming a bug while proposing to close the ticket is a
        contradiction rather than a judgement call — a real defect that gets
        resolved away is the failure mode nobody ever finds out about.
        """
        if self.disposition is Disposition.ESCALATE:
            if self.chain is None:
                raise ValueError(
                    "disposition=escalate requires an EvidenceChain; an escalation "
                    "without cited logs and cited code is a guess with a ticket number"
                )
            if not self.chain.is_linked:
                raise ValueError(
                    "disposition=escalate requires a linked EvidenceChain; the cited "
                    "code shares no terms with the cited log signature, so nothing "
                    "connects the symptom to the blamed file. Route to needs_human."
                )
        if self.intent is Intent.BUG and self.disposition is Disposition.RESOLVE:
            raise ValueError(
                "intent=bug cannot be resolved without escalation; closing a "
                "confirmed defect silently destroys the only signal that it exists"
            )
        return self

    @property
    def is_auto_closing(self) -> bool:
        """True when no human would ever see this ticket again."""
        return self.disposition is Disposition.RESOLVE


class ChallengeResult(StrictModel):
    """The challenger's ruling on a proposed auto-close."""

    ticket_id: str
    verdict: Verdict
    reasoning: str = Field(min_length=1)
    chain: EvidenceChain | None = None
    challenger_model: str | None = None
    logs_examined: int = Field(default=0, ge=0)
    files_examined: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _overturn_needs_proof(self) -> ChallengeResult:
        if self.verdict is Verdict.OVERTURNED and (self.chain is None or not self.chain.is_linked):
            raise ValueError(
                "verdict=overturned requires a linked EvidenceChain; overturning "
                "a resolution means a defect was located, so it must be quotable"
            )
        return self


# --- Patches -----------------------------------------------------------------


class PatchProposal(StrictModel):
    """What the model is allowed to propose: a before and an after.

    Deliberately *not* a unified diff. Models produce diffs with hallucinated
    context and wrong hunk offsets; asking instead for the exact text to replace
    lets `agent.patcher` locate it in the real file and generate the diff itself.
    """

    path: str
    old_text: str = Field(min_length=1)
    new_text: str
    rationale: str = Field(min_length=1)
    test_hint: str = ""


class Patch(StrictModel):
    """A proposal that has been checked against the file on disk."""

    path: str
    diff: str = ""
    applies: bool = False
    rationale: str = ""
    test_hint: str = ""
    rejection_reason: str | None = None
    anchor_sha: str | None = None

    @model_validator(mode="after")
    def _failure_must_be_explained(self) -> Patch:
        if not self.applies and not self.rejection_reason:
            raise ValueError("a patch that does not apply must record why")
        if self.applies and not self.diff.strip():
            raise ValueError("a patch that applies must carry a diff")
        return self


# --- Delivery ----------------------------------------------------------------


class JiraIssue(StrictModel):
    """The payload handed to the tracker. Strict enough for a real API call."""

    project: str
    issue_type: str = "Bug"
    summary: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    priority: str
    labels: list[str] = Field(default_factory=list)
    source_ticket: str
    components: list[str] = Field(default_factory=list)


class TrackerReceipt(StrictModel):
    """Proof that the tracker accepted the issue, and where it landed."""

    issue_key: str
    url: str
    sink: str
    created_at: datetime = Field(default_factory=_utcnow)


class TriageStep(StrictModel):
    """One entry in the run trace: what the agent did and what came back."""

    index: int = Field(ge=0)
    tool: ToolName
    arguments: dict[str, str] = Field(default_factory=dict)
    result_count: int = Field(default=0, ge=0)
    note: str = ""


class TriageReport(StrictModel):
    """The deliverable for one ticket."""

    report_id: str = Field(default_factory=lambda: f"trg_{uuid4().hex[:10]}")
    ticket_id: str
    generated_at: datetime = Field(default_factory=_utcnow)
    classification: Classification
    diagnosis: Diagnosis
    challenge: ChallengeResult | None = None
    final_disposition: Disposition
    patch: Patch | None = None
    jira_issue: JiraIssue | None = None
    receipt: TrackerReceipt | None = None
    customer_reply: str | None = None
    risk_score: float = Field(default=0.0, ge=0.0, le=100.0)
    trace: list[TriageStep] = Field(default_factory=list)
    model_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _escalation_needs_a_ticket(self) -> TriageReport:
        if self.final_disposition is Disposition.ESCALATE and self.jira_issue is None:
            raise ValueError(
                "final_disposition=escalate requires a JiraIssue; an escalation that "
                "reaches no tracker is indistinguishable from a dropped ticket"
            )
        return self

    @property
    def was_overturned(self) -> bool:
        """Whether the challenger stopped an auto-close. Justifies its cost."""
        return self.challenge is not None and self.challenge.verdict is Verdict.OVERTURNED
