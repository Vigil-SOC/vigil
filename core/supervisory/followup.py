"""Whether results got better after a suggested change was applied.

Read and written by the scheduled check. An apply is judged once,
``WINDOW + LABEL_LAG`` after it: the ``WINDOW`` before the apply against the
``WINDOW`` after it, on the function the suggestion was for. A label counts only
if a person gave it within ``LABEL_LAG`` of its window's end, so both windows
had the same time to be judged. Each side needs ``FLOOR`` labelled examples;
with fewer, ``improved`` stays null and the note says not enough yet. A change
that was undone is left alone: no verdict, no note.

What is counted, per function, by the time each item happened:

- triage: findings first triaged in the window that a person judged (a noise
  mark, or a Case they closed as real or not). Missed: Vigil said benign, the
  person said real. False alarm: Vigil said malicious or suspicious, the person
  said not real. An undetermined verdict is neither.
- investigation: an investigation's own runs that ended in the window, on a Case
  a person then closed as real or not. Missed: the Case was real and the run
  gave no answer that said so (it stopped short of completing, ran out of turns,
  or concluded benign or a false positive). False alarm: the Case was not real
  and the run completed without dismissing it. Cost and time are the mean cost
  and run time of all those runs in the window, labelled or not.
- response: actions Vigil proposed in the window that a person approved or
  rejected. False alarm: rejected, or executed and then undone. Nothing here is
  a missed attack, and there is no cost or time.

Sample alerts and probes are left out, as the evidence counts leave them out.

Judged in Gray's order, first difference wins: missed attacks, then false
alarms, then cost, then time. Missed attacks and false alarms differ only by
``COUNT_MARGIN`` or more; cost and time by ``SHARE_MARGIN`` of the before value.
No difference reads not improved, with the note saying no clear change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, and_, cast, exists, func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from core.cases.closure import PERSON_CHANNELS, ClosureCategory, closure_says_real
from core.detections.inventory.accurate import _closes, determination_of
from core.storage.models import Case, Finding
from core.storage.models.auth import Principal
from core.storage.models.base import case_findings
from core.storage.models.case_entities import CaseClosureInfo
from core.storage.models.supervisory import SupervisoryHistory, SupervisorySuggestion
from core.storage.models.workflow import ApprovalAction, Investigation, WorkflowRun
from core.storage.unit_of_work import unit_of_work
from core.supervisory.feedback import _DECIDED, _NOT_REAL_DATA, FLOOR
from core.supervisory.store import _lock

WINDOW = timedelta(days=14)
LABEL_LAG = timedelta(days=7)
COUNT_MARGIN = 2
SHARE_MARGIN = 0.10
# Applies judged per tick; the rest wait for the next.
PER_TICK = 20

_SAID_REAL = ("malicious", "suspicious")
_SAID_NOT_REAL = ("benign",)
_REAL = tuple(c.value for c in ClosureCategory if closure_says_real(c.value) is True)
_NOT_REAL = tuple(
    c.value for c in ClosureCategory if closure_says_real(c.value) is False
)
_DISMISSED = ("agent_run_closed", "agent_run_left_open")
_SHADOW_KIND = "adjudicate"


@dataclass(frozen=True)
class Measure:
    labelled: int
    missed: int
    false_alarms: int
    cost: float | None = None
    seconds: float | None = None


def follow_up(now: datetime | None = None) -> dict[str, int]:
    """Judge each apply that is due; returns how many were judged, and how."""
    now = now or datetime.now(UTC)
    done = {"improved": 0, "not_improved": 0, "not_enough": 0}
    with unit_of_work() as session:
        due = session.execute(_due(now)).all()
    for history_id, key, fn, at in due:
        with unit_of_work() as session:
            measure = _MEASURES[fn]
            before = measure(session, at - WINDOW, at, at + LABEL_LAG)
            after = measure(session, at, at + WINDOW, at + WINDOW + LABEL_LAG)
        improved, note = judge(before, after)
        if not _write(history_id, key, improved, note):
            continue
        if improved is None:
            done["not_enough"] += 1
        elif improved:
            done["improved"] += 1
        else:
            done["not_improved"] += 1
    return done


def _due(now: datetime):
    """Applies still applied, not yet judged, whose after window has been labelled."""
    return (
        select(
            SupervisoryHistory.id,
            SupervisoryHistory.setting_key,
            SupervisorySuggestion.fn,
            SupervisoryHistory.at,
        )
        .join(
            SupervisorySuggestion,
            SupervisorySuggestion.suggestion_id == SupervisoryHistory.suggestion_id,
        )
        .where(_unjudged(), SupervisoryHistory.at <= now - (WINDOW + LABEL_LAG))
        .order_by(SupervisoryHistory.at, SupervisoryHistory.id)
        .limit(PER_TICK)
    )


def _unjudged():
    """An apply of a suggestion still applied, with no verdict and no note yet."""
    return and_(
        SupervisoryHistory.outcome == "applied",
        SupervisoryHistory.improved.is_(None),
        SupervisoryHistory.note == "",
        SupervisorySuggestion.state == "applied",
    )


def _write(history_id: int, key: str, improved: bool | None, note: str) -> bool:
    """Under the setting's lock, write the verdict if the apply is still unjudged."""
    with unit_of_work() as session:
        _lock(session, key)
        row = session.execute(
            select(SupervisoryHistory)
            .join(
                SupervisorySuggestion,
                SupervisorySuggestion.suggestion_id == SupervisoryHistory.suggestion_id,
            )
            .where(SupervisoryHistory.id == history_id, _unjudged())
            .with_for_update(of=SupervisoryHistory)
        ).scalar_one_or_none()
        if row is None:
            return False
        row.improved, row.note = improved, note
        return True


def judge(before: Measure, after: Measure) -> tuple[bool | None, str]:
    """Better, worse or no clear change, first difference wins, and a short note."""
    if before.labelled < FLOOR or after.labelled < FLOOR:
        return None, (
            f"Not enough yet: {before.labelled} judged before and "
            f"{after.labelled} after; {FLOOR} each needed"
        )
    for word, b, a in (
        ("missed attacks", before.missed, after.missed),
        ("false alarms", before.false_alarms, after.false_alarms),
    ):
        if abs(a - b) >= COUNT_MARGIN:
            return a < b, f"{'Fewer' if a < b else 'More'} {word}: {b} → {a}"
    for word, b, a, unit in (
        ("cost per run", before.cost, after.cost, "$"),
        ("time per run", before.seconds, after.seconds, "s"),
    ):
        if b is None or a is None or b <= 0:
            continue
        if abs(a - b) >= SHARE_MARGIN * b:
            return a < b, (
                f"{'Lower' if a < b else 'Higher'} {word}: "
                f"{_amount(b, unit)} → {_amount(a, unit)}"
            )
    return False, (
        f"No clear change: missed attacks {before.missed} → {after.missed}, "
        f"false alarms {before.false_alarms} → {after.false_alarms}"
    )


def _amount(value: float, unit: str) -> str:
    return f"${value:.2f}" if unit == "$" else f"{value:.0f}s"


def _triage(
    session: Session, start: datetime, end: datetime, cutoff: datetime
) -> Measure:
    said = Finding.ai_enrichment["ai_triage"]["result"]["verdict"].astext
    where = (
        Finding.first_verdict_at >= _naive(start),
        Finding.first_verdict_at < _naive(end),
        Finding.data_source.notin_(_NOT_REAL_DATA),
        said.in_(_SAID_REAL + _SAID_NOT_REAL + ("undetermined",)),
        or_(Finding.noise_marked_at.isnot(None), _in_person_closed_case()),
    )
    closes = _closes(session, *where, sources=None)
    rows = session.execute(
        select(
            Finding.finding_id,
            said,
            Finding.noise_marked_at,
            Finding.noise_marked_by_kind,
        ).where(*where)
    )
    labelled = missed = false_alarms = 0
    for fid, verdict, marked, kind in rows:
        known = [c for c in closes.get(fid, []) if c.at <= cutoff]
        if marked is not None and _aware(marked) > cutoff:
            marked = None
        won = determination_of(known, marked, kind)
        if won is None or not won.analyst:
            continue
        labelled += 1
        if won.real and verdict in _SAID_NOT_REAL:
            missed += 1
        elif not won.real and verdict in _SAID_REAL:
            false_alarms += 1
    return Measure(labelled, missed, false_alarms)


def _in_person_closed_case():
    link = case_findings.alias("followup_link")
    closure = aliased(CaseClosureInfo, name="followup_closure")
    return (
        select(link.c.finding_id)
        .join(closure, closure.case_id == link.c.case_id)
        .where(
            link.c.finding_id == Finding.finding_id,
            closure.close_channel.in_(PERSON_CHANNELS),
        )
        .correlate(Finding)
        .exists()
    )


def _investigation(
    session: Session, start: datetime, end: datetime, cutoff: datetime
) -> Measure:
    closure = CaseClosureInfo
    judged = and_(
        closure.case_id.isnot(None),
        closure.closed_at >= WorkflowRun.finished_at,
        closure.closed_at <= _naive(cutoff),
    )
    real = and_(judged, closure.closure_category.in_(_REAL))
    not_real = and_(judged, closure.closure_category.in_(_NOT_REAL))
    dismissed = or_(
        *(
            Case.activities.op("@>")(
                func.jsonb_build_array(
                    func.jsonb_build_object(
                        "activity_type",
                        literal(kind),
                        "details",
                        func.jsonb_build_object("run_id", WorkflowRun.run_id),
                    )
                )
            )
            for kind in _DISMISSED
        )
    )
    completed = and_(WorkflowRun.outcome == "completed", WorkflowRun.capped.isnot(True))
    sample = exists().where(
        case_findings.c.case_id == Investigation.case_id,
        case_findings.c.finding_id == Finding.finding_id,
        Finding.data_source.in_(_NOT_REAL_DATA),
    )
    row = session.execute(
        select(
            func.count().filter(or_(real, not_real)),
            func.count().filter(and_(real, or_(~completed, dismissed))),
            func.count().filter(and_(not_real, completed, ~dismissed)),
            func.avg(WorkflowRun.total_cost_usd),
            func.avg(WorkflowRun.duration_ms),
        )
        .select_from(WorkflowRun)
        .join(
            Investigation,
            Investigation.investigation_id
            == WorkflowRun.trigger_context["investigation_id"].astext,
        )
        .outerjoin(Case, Case.case_id == Investigation.case_id)
        .outerjoin(
            closure,
            and_(
                closure.case_id == Investigation.case_id,
                closure.close_channel.in_(PERSON_CHANNELS),
            ),
        )
        .where(
            WorkflowRun.finished_at >= _naive(start),
            WorkflowRun.finished_at < _naive(end),
            WorkflowRun.deleted_at.is_(None),
            func.coalesce(WorkflowRun.trigger_context["run_kind"].astext, "")
            != _SHADOW_KIND,
            ~sample,
        )
    ).one()
    labelled, missed, false_alarms, cost, ms = row
    return Measure(
        labelled,
        missed,
        false_alarms,
        float(cost) if cost is not None else None,
        float(ms) / 1000 if ms is not None else None,
    )


def _response(
    session: Session, start: datetime, end: datetime, cutoff: datetime
) -> Measure:
    proposer = aliased(Principal)
    person_proposed = exists().where(
        proposer.principal_id == ApprovalAction.created_by_principal_id,
        proposer.kind == "person",
    )
    undone_at = cast(ApprovalAction.execution_result["undone_at"].astext, DateTime)
    undone = and_(
        ApprovalAction.status == "executed",
        undone_at.isnot(None),
        undone_at <= _naive(cutoff),
    )
    labelled, false_alarms = session.execute(
        select(
            func.count(),
            func.count().filter(or_(ApprovalAction.status == "rejected", undone)),
        )
        .join(
            Principal,
            and_(
                Principal.principal_id == ApprovalAction.approved_by_principal_id,
                Principal.kind == "person",
            ),
        )
        .where(
            ApprovalAction.created_at >= _naive(start),
            ApprovalAction.created_at < _naive(end),
            ApprovalAction.status.in_(_DECIDED),
            ApprovalAction.approved_at <= _naive(cutoff),
            ApprovalAction.parameters["undoes"].astext.is_(None),
            ~person_proposed,
        )
    ).one()
    return Measure(labelled, 0, false_alarms)


_MEASURES: dict[str, Any] = {
    "triage": _triage,
    "investigation": _investigation,
    "response": _response,
}


def _naive(value: datetime) -> datetime:
    """UTC without tzinfo, as the measured columns store it."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
