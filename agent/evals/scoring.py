"""Golden answer keys and the metrics computed against them.

The positive class is **"this ticket is a real defect and must reach
engineering"**, so:

- a false positive is telling a developer to investigate something that works;
- a false negative is a defect that did not reach engineering.

Those are not equally bad, and the metrics do not pretend they are. A false
negative splits two ways and only one of them is a disaster:

- **routed to a human** — expensive, slow, and safe. The ticket is still open.
- **silently closed** — the customer got a help article, the ticket is shut, and
  the only report of the defect is gone.

`silent_closures` is therefore reported separately and is the number to look at
first. Precision and recall are there for comparison between configurations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import yaml

from agent.config import AGENT_DIR
from agent.schemas import Disposition, Intent, TriageReport

GOLDEN_DIR = AGENT_DIR / "evals" / "golden"


@dataclass(slots=True)
class GoldenKey:
    """The answer for one ticket. Written by hand, never by the system."""

    ticket_id: str
    intent: Intent
    disposition: Disposition
    root_cause_paths: list[str] = field(default_factory=list)
    related_paths: list[str] = field(default_factory=list)
    trap: str | None = None
    notes: str = ""

    @property
    def is_defect(self) -> bool:
        return self.disposition is Disposition.ESCALATE

    def scores_attribution(self) -> bool:
        """Whether this ticket has a right answer for *which file*."""
        return bool(self.root_cause_paths)


@lru_cache(maxsize=1)
def load_keys() -> dict[str, GoldenKey]:
    keys: dict[str, GoldenKey] = {}
    for path in sorted(GOLDEN_DIR.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        key = GoldenKey(
            ticket_id=raw["ticket_id"],
            intent=Intent(raw["intent"]),
            disposition=Disposition(raw["disposition"]),
            root_cause_paths=list(raw.get("root_cause_paths") or []),
            related_paths=list(raw.get("related_paths") or []),
            trap=raw.get("trap"),
            notes=(raw.get("notes") or "").strip(),
        )
        keys[key.ticket_id] = key
    return keys


@dataclass(slots=True)
class TicketOutcome:
    """How one ticket went, scored against its key."""

    ticket_id: str
    trap: str | None
    expected: Disposition
    actual: Disposition
    expected_intent: Intent
    actual_intent: Intent
    cited_paths: list[str]
    attribution: str  # "correct" | "related" | "wrong" | "n/a"
    overturned: bool
    fabrications: int

    @property
    def disposition_correct(self) -> bool:
        return self.expected is self.actual

    @property
    def silent_closure(self) -> bool:
        """A defect that was closed. The failure with no second chance."""
        return self.expected is Disposition.ESCALATE and self.actual is Disposition.RESOLVE

    @property
    def contained(self) -> bool:
        """A defect that at least reached a human instead of being closed."""
        return self.expected is Disposition.ESCALATE and self.actual is Disposition.NEEDS_HUMAN

    @property
    def false_escalation(self) -> bool:
        return self.expected is not Disposition.ESCALATE and self.actual is Disposition.ESCALATE


def score_report(report: TriageReport) -> TicketOutcome:
    key = load_keys()[report.ticket_id]
    chain = report.diagnosis.chain
    cited = sorted({s.path for s in chain.code_spans}) if chain else []

    attribution = "n/a"
    if key.scores_attribution() and report.final_disposition is Disposition.ESCALATE:
        if any(p in key.root_cause_paths for p in cited):
            attribution = "correct"
        elif any(p in key.related_paths for p in cited):
            attribution = "related"
        else:
            attribution = "wrong"

    return TicketOutcome(
        ticket_id=report.ticket_id,
        trap=key.trap,
        expected=key.disposition,
        actual=report.final_disposition,
        expected_intent=key.intent,
        actual_intent=report.diagnosis.intent,
        cited_paths=cited,
        attribution=attribution,
        overturned=report.was_overturned,
        fabrications=len(report.diagnosis.fabrications),
    )


@dataclass(slots=True)
class Metrics:
    """Aggregate results for one configuration."""

    label: str
    outcomes: list[TicketOutcome]

    @property
    def true_positives(self) -> int:
        return sum(
            1
            for o in self.outcomes
            if o.expected is Disposition.ESCALATE and o.actual is Disposition.ESCALATE
        )

    @property
    def false_positives(self) -> int:
        return sum(1 for o in self.outcomes if o.false_escalation)

    @property
    def false_negatives(self) -> int:
        return sum(
            1
            for o in self.outcomes
            if o.expected is Disposition.ESCALATE and o.actual is not Disposition.ESCALATE
        )

    @property
    def silent_closures(self) -> int:
        """The number that matters. Defects nobody will ever see again."""
        return sum(1 for o in self.outcomes if o.silent_closure)

    @property
    def contained(self) -> int:
        return sum(1 for o in self.outcomes if o.contained)

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def disposition_accuracy(self) -> float:
        if not self.outcomes:
            return 0.0
        return sum(1 for o in self.outcomes if o.disposition_correct) / len(self.outcomes)

    @property
    def attribution_correct(self) -> int:
        return sum(1 for o in self.outcomes if o.attribution == "correct")

    @property
    def attribution_scored(self) -> int:
        return sum(1 for o in self.outcomes if o.attribution != "n/a")

    @property
    def overturns(self) -> int:
        return sum(1 for o in self.outcomes if o.overturned)

    @property
    def correct_overturns(self) -> int:
        """Overturns that rescued a genuine defect. The rest are noise at best."""
        return sum(
            1 for o in self.outcomes if o.overturned and o.expected is Disposition.ESCALATE
        )

    @property
    def fabrications(self) -> int:
        return sum(o.fabrications for o in self.outcomes)

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "tickets": len(self.outcomes),
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
            "true_positives": self.true_positives,
            "false_positives": self.false_positives,
            "false_negatives": self.false_negatives,
            "silent_closures": self.silent_closures,
            "contained": self.contained,
            "disposition_accuracy": round(self.disposition_accuracy, 3),
            "attribution_correct": self.attribution_correct,
            "attribution_scored": self.attribution_scored,
            "overturns": self.overturns,
            "correct_overturns": self.correct_overturns,
            "fabrications": self.fabrications,
        }
