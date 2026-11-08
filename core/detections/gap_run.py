"""What the daily detection gaps run raises: the gaps in identify_gaps' report that are
new since the run's stored marker, at or above a floor, not already covered by an
open Case, ranked by count. Plain code over the report; the agent the run queues
reads the full report again itself."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Set

from core.cases.case_status import CLOSED
from core.storage.connection import get_db_manager
from core.storage.models import Case, SystemConfig

MARKER_KEY = "detection_gaps.marker"
# Defaults, not settings yet.
# A missed-runs or paper-only gap is raised at this count or above; one
# analyst-confirmed missed run is enough by itself, as identify_gaps lists a group
# with 2+ runs or 1+ confirmed. A blind spot is a standing state, not a count of
# sightings: it is raised once whatever it starves, and ranked by that.
GAP_FLOOR = 2
# The first run, with no marker, reads this far back.
FIRST_RUN_DAYS = 30

MISSED, PAPER, BLIND = "missed", "paper", "blind"
_LABEL = {
    MISSED: "runs no rule caught",
    PAPER: "covered on paper only",
    BLIND: "blind spot (data source)",
}
_UNIT = {MISSED: "run", PAPER: "alert", BLIND: "rule starved"}
_KIND_ORDER = {MISSED: 0, PAPER: 1, BLIND: 2}


@dataclass(frozen=True)
class Marker:
    """Where the last successful run stopped, and the blind spots it already raised
    (a blind spot is a standing state with no time, so the marker remembers it)."""

    since: datetime
    blind_spots: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class Gap:
    kind: str
    # The technique, or the data source of a blind spot.
    subject: str
    # Missed runs, alerts on a paper-only technique, or rules a blind spot starves.
    count: int
    confirmed: int = 0

    def line(self) -> str:
        unit = _UNIT[self.kind] if self.count == 1 else _plural(_UNIT[self.kind])
        confirmed = f", {self.confirmed} confirmed" if self.confirmed else ""
        return f"{self.subject} {_LABEL[self.kind]} ({self.count} {unit}{confirmed})"


def _plural(unit: str) -> str:
    return "rules starved" if unit == "rule starved" else f"{unit}s"


def read_marker(now: datetime) -> Marker:
    """The stored marker; with none, the first run's window back from ``now``."""
    with get_db_manager().session_scope() as session:
        row = session.get(SystemConfig, MARKER_KEY)
        value = dict(row.value) if row else None
    if not value or not value.get("since"):
        return Marker(now - timedelta(days=FIRST_RUN_DAYS))
    since = datetime.fromisoformat(value["since"])
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return Marker(since, frozenset(value.get("blind_spots") or ()))


def write_marker(marker: Marker) -> None:
    """Store the marker. Run state, not configuration: no config audit row."""
    value = {
        "since": marker.since.isoformat(),
        "blind_spots": sorted(marker.blind_spots),
    }
    with get_db_manager().session_scope() as session:
        row = session.get(SystemConfig, MARKER_KEY)
        if row is None:
            session.add(
                SystemConfig(
                    key=MARKER_KEY,
                    value=value,
                    description="Where the daily detection gaps run last stopped",
                    config_type="detections",
                    updated_by="system",
                )
            )
        else:
            row.value = value
            row.updated_by = "system"


def open_case_techniques() -> Set[str]:
    """Techniques an open Case already carries: a gap on one is not raised again."""
    with get_db_manager().session_scope() as session:
        rows = session.query(Case.mitre_techniques).filter(Case.status != CLOSED)
        return {
            t.strip().upper() for (techniques,) in rows for t in techniques or () if t
        }


def gaps_in(report: Dict[str, Any]) -> List[Gap]:
    """Every gap the report lists, with its count."""
    gaps = [
        Gap(MISSED, g["technique"], len(g["runs"]), g.get("confirmed", 0))
        for g in report["runs_rules_missed"]["groups"]
    ]
    alerts = {row["technique"]: row["alerts"] for row in report.get("techniques", [])}
    gaps += [
        Gap(PAPER, technique, alerts.get(technique, 0))
        for technique in report["covered_on_paper_only"]
    ]
    gaps += [
        Gap(BLIND, spot["data_source"], len(spot.get("rules") or ()))
        for spot in report["blind_spots"]
    ]
    return gaps


def to_raise(
    gaps: Iterable[Gap],
    marker: Marker,
    covered: Set[str],
    floor: int = GAP_FLOOR,
) -> List[Gap]:
    """The gaps to raise, biggest first: a technique at or above the floor (or
    confirmed) that no open Case carries, and a blind spot not raised before."""
    raised = []
    for gap in gaps:
        if gap.kind == BLIND:
            if gap.subject not in marker.blind_spots:
                raised.append(gap)
        elif gap.subject.upper() not in covered and (
            gap.count >= floor or gap.confirmed
        ):
            raised.append(gap)
    return sorted(raised, key=lambda g: (-g.count, _KIND_ORDER[g.kind], g.subject))


def next_marker(
    started: datetime, marker: Marker, gaps: Iterable[Gap], raised: Iterable[Gap]
) -> Marker:
    """The marker after a successful run: its start (so what arrived during the run is
    read next time), and the blind spots raised now or before that are still open."""
    still_open = {g.subject for g in gaps if g.kind == BLIND}
    remembered = marker.blind_spots | {g.subject for g in raised if g.kind == BLIND}
    return Marker(started, frozenset(remembered & still_open))


def case_techniques(raised: Iterable[Gap]) -> List[str]:
    """The techniques the run's Case carries, so the next run sees them covered."""
    return sorted({g.subject.upper() for g in raised if g.kind != BLIND})
