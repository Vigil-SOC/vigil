"""What the daily detection gaps run raises: the gaps in identify_gaps' report over a
rolling window, counting only evidence no earlier run raised, at or above a floor,
not on a technique an open Case already carries, ranked by count. Plain code over the
report; the agent the run queues reads the full report again itself.

The run remembers what it raised, not when it last ran: each missed run and each
alert it raised (or found covered by an open Case), by id, and each blind spot. A
window re-read each day catches what a time marker would skip for good: evidence
committed after a run started, an alert labelled after it was stored, and sightings
that reach the floor only over several days."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Set, Tuple

from core.cases.case_status import CLOSED
from core.detections.gaps import TECHNIQUE_ID, read_alert_labels
from core.storage.connection import get_db_manager
from core.storage.models import Case, SystemConfig

RAISED_KEY = "detection_gaps.raised"
# Defaults, not settings yet.
# How far back each run reads alerts and Verdicts.
WINDOW_DAYS = 30
# A missed-runs or paper-only gap is raised once it has this many runs or alerts in
# the window that no run raised yet; one analyst-confirmed new run is enough by
# itself, as identify_gaps lists a group with 2+ runs or 1+ confirmed. A blind spot
# is a standing state, not a count of sightings: it is raised once whatever it
# starves, and ranked by that.
GAP_FLOOR = 2

MISSED, PAPER, BLIND = "missed", "paper", "blind"
_LABEL = {
    MISSED: "runs no rule caught",
    PAPER: "covered on paper only",
    BLIND: "blind spot (data source)",
}
_UNIT = {MISSED: ("run", "runs"), PAPER: ("alert", "alerts")}
_KIND_ORDER = {MISSED: 0, PAPER: 1, BLIND: 2}


@dataclass(frozen=True)
class Raised:
    """What earlier runs raised: run keys ("kind:id") and alert ids, each with when,
    and the blind spots raised and still open."""

    runs: Mapping[str, str] = field(default_factory=dict)
    alerts: Mapping[str, str] = field(default_factory=dict)
    blind_spots: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class Gap:
    kind: str
    # The technique, or the data source of a blind spot.
    subject: str
    # New missed runs, new alerts on a paper-only technique, or rules a blind spot starves.
    count: int
    confirmed: int = 0
    # The run keys or alert ids counted, recorded once the gap is raised.
    evidence: Tuple[str, ...] = ()

    def line(self) -> str:
        if self.kind == BLIND:
            n = f"{self.count} rule{'' if self.count == 1 else 's'} starved"
        else:
            one, many = _UNIT[self.kind]
            n = f"{self.count} new {one if self.count == 1 else many}"
        confirmed = f", {self.confirmed} confirmed" if self.confirmed else ""
        return f"{self.subject} {_LABEL[self.kind]} ({n}{confirmed})"


def read_raised() -> Raised:
    with get_db_manager().session_scope() as session:
        row = session.get(SystemConfig, RAISED_KEY)
        value = dict(row.value) if row else {}
    return Raised(
        dict(value.get("runs") or {}),
        dict(value.get("alerts") or {}),
        frozenset(value.get("blind_spots") or ()),
    )


def write_raised(raised: Raised) -> None:
    """Store what was raised. Run state, not configuration: no config audit row."""
    value = {
        "runs": dict(raised.runs),
        "alerts": dict(raised.alerts),
        "blind_spots": sorted(raised.blind_spots),
    }
    with get_db_manager().session_scope() as session:
        row = session.get(SystemConfig, RAISED_KEY)
        if row is None:
            session.add(
                SystemConfig(
                    key=RAISED_KEY,
                    value=value,
                    description="What the daily detection gaps run has raised",
                    config_type="detections",
                    updated_by="system",
                )
            )
        else:
            row.value = value
            row.updated_by = "system"


def alert_ids(since: datetime) -> Dict[str, List[str]]:
    """Each technique's alerts stored since ``since``, by finding id, labelled as
    identify_gaps reads them (whenever the label was written)."""
    out: Dict[str, List[str]] = {}
    for finding_id, labels in read_alert_labels("", since).by_alert.items():
        for technique in {
            label.strip().upper()
            for label in labels
            if TECHNIQUE_ID.match(label.strip())
        }:
            out.setdefault(technique, []).append(finding_id)
    return out


def open_case_techniques() -> Set[str]:
    """Techniques an open Case already carries: a gap on one is not raised again."""
    with get_db_manager().session_scope() as session:
        rows = session.query(Case.mitre_techniques).filter(Case.status != CLOSED)
        return {
            t.strip().upper() for (techniques,) in rows for t in techniques or () if t
        }


def _run_key(run: Mapping[str, Any]) -> str:
    return f"{run['investigation_kind']}:{run['investigation_id']}"


def gaps_in(
    report: Dict[str, Any], alerts: Mapping[str, List[str]], raised: Raised
) -> List[Gap]:
    """Every gap the report lists, counting only evidence no run raised yet."""
    gaps = []
    for group in report["runs_rules_missed"]["groups"]:
        new = [_run_key(r) for r in group["runs"] if _run_key(r) not in raised.runs]
        confirmed = [
            k
            for k in (_run_key(r) for r in group.get("confirmed_runs") or ())
            if k not in raised.runs
        ]
        gaps.append(
            Gap(MISSED, group["technique"], len(new), len(confirmed), tuple(new))
        )
    for technique in report["covered_on_paper_only"]:
        new = tuple(
            sorted(
                f for f in alerts.get(technique.upper(), ()) if f not in raised.alerts
            )
        )
        gaps.append(Gap(PAPER, technique, len(new), 0, new))
    gaps += [
        Gap(BLIND, spot["data_source"], len(spot.get("rules") or ()))
        for spot in report["blind_spots"]
    ]
    return gaps


def to_raise(
    gaps: Iterable[Gap],
    raised: Raised,
    covered: Set[str],
    floor: int = GAP_FLOOR,
) -> Tuple[List[Gap], List[Gap]]:
    """(the gaps to raise, biggest first; the technique gaps an open Case covers).
    A technique is raised at or above the floor of new evidence (or one new
    confirmed run); a blind spot when no run raised it while it stayed open."""
    to, held = [], []
    for gap in gaps:
        if gap.kind == BLIND:
            if gap.subject not in raised.blind_spots:
                to.append(gap)
        elif gap.subject.upper() in covered:
            if gap.evidence:
                held.append(gap)
        elif gap.count >= floor or gap.confirmed:
            to.append(gap)
    to.sort(key=lambda g: (-g.count, _KIND_ORDER[g.kind], g.subject))
    return to, held


def after_run(
    raised: Raised,
    started: datetime,
    window_start: datetime,
    gaps: Iterable[Gap],
    to: Iterable[Gap],
    held: Iterable[Gap],
) -> Raised:
    """What is remembered once the run succeeds: the evidence it raised, and what an
    open Case covered (that Case is looking at it), at ``started``; what was raised
    before the window drops out (its evidence is older, so the window can't hold it
    again); a blind spot stays while it is still open."""
    at = started.isoformat()
    start = window_start.isoformat()
    runs = {k: v for k, v in raised.runs.items() if v >= start}
    alerts = {k: v for k, v in raised.alerts.items() if v >= start}
    for gap in list(to) + list(held):
        bucket = runs if gap.kind == MISSED else alerts if gap.kind == PAPER else None
        if bucket is not None:
            bucket.update({e: at for e in gap.evidence})
    still_open = {g.subject for g in gaps if g.kind == BLIND}
    blind = (
        raised.blind_spots | {g.subject for g in to if g.kind == BLIND}
    ) & still_open
    return Raised(runs, alerts, frozenset(blind))


def case_techniques(raised: Iterable[Gap]) -> List[str]:
    """The techniques the run's Case carries, so the next run sees them covered."""
    return sorted({g.subject.upper() for g in raised if g.kind != BLIND})
