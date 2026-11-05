"""Compute what normal looks like from history a customer already has.

Pure functions over plain alerts and closed tickets, no database and no model.
Nothing here closes an alert. ``assess`` returns a recommendation and its reasons.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    Mapping,
    Optional,
    Tuple,
)

BENIGN_DISPOSITIONS = frozenset({"benign_positive", "false_positive"})
TRUE_POSITIVE = "true_positive"

# Labels and history a clean signature needs before it can be a dismiss candidate.
MIN_CLEAN_LABELS = 5
# With no tickets, frequency is the only evidence. A repeatedly phished user also fires
# often, so frequency alone cannot clear anything above low.
ROUTINE_SEVERITIES = frozenset({"info", "low"})
MIN_ROUTINE_ACTIVE_DAYS = 5
MIN_ENTITY_SIGHTINGS = 3
# Enough clean tickets that a new user or host on this kind of alert stays routine: 59
# bounds the kind at 5% real. At 20 (a 13.9% bound) a short ticket window closed real
# alerts on workstations the kind had never fired on.
MIN_STRONG_LABELS = 59
STRONG_SEVERITIES = frozenset({"info", "low", "medium"})
MISS_CONFIDENCE = 0.05

_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# Only hash-length ids vary within one detection. Shorter codes can name the detection.
_HEX = re.compile(r"\b[0-9a-f]{16,}\b", re.I)


class Stage1(str, Enum):
    ESCALATE = "escalate"
    ABSTAIN = "abstain"
    DISMISS_CANDIDATE = "dismiss_candidate"


@dataclass(frozen=True)
class Alert:
    alert_id: str
    source: str
    title: str
    severity: str
    observed_at: datetime
    entities: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class Outcome:
    """A closed ticket's answer, already filtered to labels worth trusting."""

    signature: str
    disposition: str
    closed_at: datetime
    entities: FrozenSet[str] = frozenset()
    ticket_id: Optional[str] = None


@dataclass
class SignatureBaseline:
    signature: str
    count: int = 0
    active_days: int = 0
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    entity_counts: Counter = field(default_factory=Counter)
    entity_benign: Counter = field(default_factory=Counter)
    entity_true_positive: Counter = field(default_factory=Counter)
    benign: int = 0
    true_positive: int = 0
    marked_not_benign: bool = False

    @property
    def labelled(self) -> int:
        return self.benign + self.true_positive

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signature": self.signature,
            "count": self.count,
            "active_days": self.active_days,
            "first_seen": _iso(self.first_seen),
            "last_seen": _iso(self.last_seen),
            "entity_counts": dict(self.entity_counts),
            "entity_benign": dict(self.entity_benign),
            "entity_true_positive": dict(self.entity_true_positive),
            "benign": self.benign,
            "true_positive": self.true_positive,
            "marked_not_benign": self.marked_not_benign,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SignatureBaseline":
        return cls(
            signature=data["signature"],
            count=data["count"],
            active_days=data["active_days"],
            first_seen=parse_time(data["first_seen"]),
            last_seen=parse_time(data["last_seen"]),
            entity_counts=Counter(data["entity_counts"]),
            entity_benign=Counter(data["entity_benign"]),
            entity_true_positive=Counter(data["entity_true_positive"]),
            benign=data["benign"],
            true_positive=data["true_positive"],
            marked_not_benign=data.get("marked_not_benign", False),
        )


@dataclass
class Baseline:
    as_of: datetime
    signatures: Dict[str, SignatureBaseline]
    alerts_seen: int
    days_covered: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "signatures": {k: v.to_dict() for k, v in self.signatures.items()},
            "alerts_seen": self.alerts_seen,
            "days_covered": self.days_covered,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Baseline":
        return cls(
            as_of=parse_time(data["as_of"]),
            signatures={
                k: SignatureBaseline.from_dict(v) for k, v in data["signatures"].items()
            },
            alerts_seen=data["alerts_seen"],
            days_covered=data["days_covered"],
        )


@dataclass(frozen=True)
class Assessment:
    stage1: Stage1
    rule: str
    reasons: Tuple[str, ...]


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# The adapters write dest_ips; LogLM rows and older findings carry one value.
_ENTITY_KINDS = (
    ("user", ("usernames", "users", "user")),
    ("ip", ("src_ips", "dst_ips", "dest_ips", "src_ip", "dst_ip")),
    ("host", ("hostnames", "hosts", "hostname")),
)
ENTITY_KEYS = tuple(key for _, keys in _ENTITY_KINDS for key in keys)


def _entities(finding: Mapping[str, Any]) -> FrozenSet[str]:
    context = finding.get("iocs") or finding.get("entity_context") or {}
    if not isinstance(context, Mapping):
        return frozenset()
    found = set()
    for kind, keys in _ENTITY_KINDS:
        for key in keys:
            values = context.get(key) or []
            if isinstance(values, str):
                values = [values]
            if isinstance(values, (list, tuple, set, frozenset)):
                found.update(f"{kind}:{v}" for v in values if v)
    return frozenset(found)


def alert_from_finding(finding: Mapping[str, Any]) -> Optional[Alert]:
    """An adapter or upload row as an Alert. None when it has no usable time."""
    observed = parse_time(finding.get("timestamp"))
    if observed is None:
        return None
    return Alert(
        alert_id=str(
            finding.get("finding_id")
            or finding.get("id")
            or finding.get("external_id")
            or ""
        ),
        source=str(finding.get("data_source") or finding.get("source") or "unknown"),
        title=str(finding.get("title") or finding.get("rule_name") or "Untitled alert"),
        severity=str(finding.get("severity") or "unknown").lower(),
        observed_at=observed,
        entities=_entities(finding),
    )


def signature_of(source: str, title: str, entities: Iterable[str] = ()) -> str:
    """The alert's kind: source plus title with its entities, addresses and long ids
    masked, so "Failed login for alice" and "Failed login for bob" group together. Other
    numbers stay: "Windows Event 1102" must not inherit the precedent of "Event 4624".
    """
    masked = title
    values = sorted((e.split(":", 1)[-1] for e in entities), key=len, reverse=True)
    for value in values:
        if value:
            masked = re.sub(re.escape(value), "<entity>", masked, flags=re.I)
    masked = _HEX.sub("<id>", _IP.sub("<ip>", masked))
    return f"{source}|{' '.join(masked.split())}"


def signature_for(alert: Alert) -> str:
    return signature_of(alert.source, alert.title, alert.entities)


def miss_bound(clean_labels: int) -> Optional[float]:
    """With this many clean labels and none wrong, the upper bound on how often
    this signature is really a true positive, at 95% confidence."""
    if clean_labels <= 0:
        return None
    return 1 - MISS_CONFIDENCE ** (1 / clean_labels)


def build_baseline(
    alerts: Iterable[Alert], outcomes: Iterable[Outcome], as_of: datetime
) -> Baseline:
    """Everything known strictly before ``as_of``. Later data never leaks in."""
    signatures: Dict[str, SignatureBaseline] = {}
    days: Dict[str, set] = defaultdict(set)
    all_days = set()
    seen = 0
    for alert in alerts:
        if alert.observed_at >= as_of:
            continue
        seen += 1
        key = signature_for(alert)
        row = signatures.setdefault(key, SignatureBaseline(signature=key))
        row.count += 1
        day = alert.observed_at.date()
        days[key].add(day)
        all_days.add(day)
        row.first_seen = min(row.first_seen or alert.observed_at, alert.observed_at)
        row.last_seen = max(row.last_seen or alert.observed_at, alert.observed_at)
        row.entity_counts.update(alert.entities)
    for key, row in signatures.items():
        row.active_days = len(days[key])

    # One ticket is one person's one decision, however many alerts it names. Counting
    # per alert would let a single ticket meet a bar that means 59 separate decisions.
    counted = set()
    for outcome in outcomes:
        if outcome.closed_at >= as_of:
            continue
        row = signatures.setdefault(
            outcome.signature, SignatureBaseline(signature=outcome.signature)
        )
        scope = (outcome.signature, outcome.ticket_id)
        anonymous = outcome.ticket_id is None
        is_new = anonymous or scope not in counted
        entities = [
            e for e in outcome.entities if anonymous or (*scope, e) not in counted
        ]
        counted.add(scope)
        counted.update((*scope, e) for e in outcome.entities)
        if outcome.disposition == TRUE_POSITIVE:
            row.true_positive += is_new
            row.entity_true_positive.update(entities)
        elif outcome.disposition in BENIGN_DISPOSITIONS:
            row.benign += is_new
            row.entity_benign.update(entities)
    return Baseline(
        as_of=as_of,
        signatures=signatures,
        alerts_seen=seen,
        days_covered=len(all_days),
    )


def _familiar(alert: Alert, row: SignatureBaseline) -> bool:
    """Every entity on the alert has appeared on this signature before."""
    if not alert.entities:
        return row.count >= MIN_ENTITY_SIGHTINGS
    return all(row.entity_counts[e] >= MIN_ENTITY_SIGHTINGS for e in alert.entities)


@dataclass(frozen=True)
class _Context:
    alert: Alert
    row: Optional[SignatureBaseline]
    baseline: Baseline


@dataclass(frozen=True)
class _Rule:
    name: str
    outcome: Stage1
    matches: Callable[[_Context], bool]
    why: Callable[[_Context], str]


def _bad_entities(c: _Context) -> FrozenSet[str]:
    """Entities on this alert that were in a real incident of this same kind."""
    if c.row is None:
        return frozenset()
    return frozenset(e for e in c.alert.entities if c.row.entity_true_positive[e])


def _entities_clean(c: _Context) -> bool:
    """Every entity has enough benign tickets on this kind of alert."""
    return bool(c.alert.entities) and all(
        c.row.entity_benign[e] >= MIN_CLEAN_LABELS for e in c.alert.entities
    )


# First match wins. Rules that add scrutiny run before any rule that removes it.
RULES: Tuple[_Rule, ...] = (
    _Rule(
        "marked_not_benign",
        Stage1.ESCALATE,
        lambda c: bool(c.row) and c.row.marked_not_benign,
        lambda c: "A person reviewed this kind of alert and marked it not benign.",
    ),
    _Rule(
        "critical_source_severity",
        Stage1.ESCALATE,
        lambda c: c.alert.severity == "critical",
        lambda c: "The source rated this critical.",
    ),
    _Rule(
        "entity_true_positive_precedent",
        Stage1.ESCALATE,
        lambda c: bool(_bad_entities(c)),
        lambda c: f"{', '.join(sorted(_bad_entities(c)))} was in a confirmed incident of this same kind.",
    ),
    _Rule(
        "signature_mostly_true_positive",
        Stage1.ESCALATE,
        lambda c: bool(c.row)
        and c.row.labelled >= MIN_CLEAN_LABELS
        and c.row.true_positive * 2 >= c.row.labelled,
        lambda c: f"{c.row.true_positive} of {c.row.labelled} past tickets of this kind were real.",
    ),
    _Rule(
        "new_signature",
        Stage1.ABSTAIN,
        lambda c: c.row is None or c.row.count == 0,
        lambda c: "Vigil has never seen this kind of alert in the history it has.",
    ),
    _Rule(
        "clean_on_these_entities",
        Stage1.DISMISS_CANDIDATE,
        _entities_clean,
        lambda c: f"These entities have {min(c.row.entity_benign[e] for e in c.alert.entities)} or more benign tickets on this kind of alert and no real ones.",
    ),
    _Rule(
        "signature_has_true_positives",
        Stage1.ABSTAIN,
        lambda c: c.row.true_positive > 0,
        lambda c: f"{c.row.true_positive} of {c.row.labelled} past tickets of this kind were real, so it needs judgment.",
    ),
    _Rule(
        "strong_clean_ticket_history",
        Stage1.DISMISS_CANDIDATE,
        lambda c: c.row.benign >= MIN_STRONG_LABELS
        and c.alert.severity in STRONG_SEVERITIES,
        lambda c: f"{c.row.benign} past tickets of this kind closed benign and none real. Bounds the miss rate at {miss_bound(c.row.benign):.1%}.",
    ),
    _Rule(
        "new_entity_on_signature",
        Stage1.ABSTAIN,
        lambda c: not _familiar(c.alert, c.row),
        lambda c: "This kind of alert is known, but not on these users, hosts or addresses.",
    ),
    _Rule(
        "clean_ticket_history",
        Stage1.DISMISS_CANDIDATE,
        lambda c: c.row.benign >= MIN_CLEAN_LABELS,
        lambda c: f"{c.row.benign} past tickets closed benign and none real, on the same entities. Bounds the miss rate at {miss_bound(c.row.benign):.1%}.",
    ),
    _Rule(
        "routine_recurring",
        Stage1.DISMISS_CANDIDATE,
        lambda c: c.row.active_days >= MIN_ROUTINE_ACTIVE_DAYS
        and c.alert.severity in ROUTINE_SEVERITIES,
        lambda c: f"Fired on {c.row.active_days} separate days on the same entities, with no tickets to confirm it.",
    ),
)


def assess(alert: Alert, baseline: Baseline) -> Assessment:
    context = _Context(alert, baseline.signatures.get(signature_for(alert)), baseline)
    for rule in RULES:
        if rule.matches(context):
            return Assessment(rule.outcome, rule.name, (rule.why(context),))
    return Assessment(
        Stage1.ABSTAIN, "no_rule", ("Nothing in the history decides this.",)
    )
