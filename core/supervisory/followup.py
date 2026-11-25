"""Whether results got better after a suggested change was applied.

Read and written by the scheduled check. An apply is judged once,
``WINDOW + LABEL_LAG`` after it: the ``WINDOW`` before the apply against the
``WINDOW`` after it, on the function the suggestion was for. A label counts only
if a person gave it within ``LABEL_LAG`` of its window's end, so both windows
had the same time to be judged. Each side needs ``FLOOR`` labelled examples;
with fewer, ``improved`` stays null and the note says not enough yet. A change
that was undone is left alone: no verdict, no note.

What is counted, per function, by the time each item happened:

- triage: findings whose standing triage verdict was made in the window (the
  verdict's own time, so a finding re-triaged after the apply counts after it)
  and that a person judged (a noise mark, or a Case they closed as real or not).
  Missed: Vigil said benign, the person said real. False alarm: Vigil said
  malicious or suspicious, the person said not real. Undetermined is neither.
- investigation: each Case's last investigation run that ended in the window,
  on a Case a person then closed as real or not; a run retried after a failure
  counts once, as its retry. Missed: the Case was real and the run gave no
  answer that said so: it did not complete, ran out of turns, or recorded on the
  Case that it concluded benign or a false positive. False alarm: the Case was
  not real and the run completed without recording a dismissal. Cost and time
  are the mean cost and run time of every such run in the window, labelled or
  not.
- response: actions Vigil proposed in the window that a person approved or
  rejected. False alarm: rejected, or executed and then undone. Nothing here is
  a missed attack, and there is no cost or time.

Sample alerts and probes are left out, as the evidence counts leave them out.

Judged in Gray's order, first difference wins: missed attacks, then false
alarms, then cost, then time. Missed attacks and false alarms are compared as
rates, so a busier or quieter fortnight does not read as better or worse: the
before count is scaled to the after side's labelled total, and the two differ
only by ``COUNT_MARGIN`` or more of those. Cost and time differ by
``SHARE_MARGIN`` of the before value. No difference reads not improved, with the
note saying no clear change.

An apply is unjudged while its ``improved`` is null and its note empty: the
apply path writes no note, so the note this module writes, "not enough yet"
included, marks it done.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, and_, case, cast, exists, func, literal, or_, select
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

logger = logging.getLogger(__name__)

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
_ISO = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$"


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
    done = {"improved": 0, "not_improved": 0, "not_enough": 0, "failed": 0}
    with unit_of_work() as session:
        due = session.execute(_due(now)).all()
    for history_id, key, fn, at in due:
        try:
            with unit_of_work() as session:
                measure = _MEASURES[fn]
                before = measure(session, at - WINDOW, at, at + LABEL_LAG)
                after = measure(session, at, at + WINDOW, at + WINDOW + LABEL_LAG)
            improved, note = judge(before, after)
            written = _write(history_id, key, improved, note)
        except Exception:  # noqa: BLE001 - one apply must not hold up the rest
            logger.exception("could not follow up apply %s", history_id)
            done["failed"] += 1
            continue
        if not written:
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
    scale = after.labelled / before.labelled
    for word, b, a in (
        ("missed attacks", before.missed, after.missed),
        ("false alarms", before.false_alarms, after.false_alarms),
    ):
        if abs(a - b * scale) >= COUNT_MARGIN:
            return a < b * scale, (
                f"{'Fewer' if a < b * scale else 'More'} {word}: "
                f"{b} of {before.labelled} → {a} of {after.labelled}"
            )
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
        f"No clear change: missed attacks {before.missed} of {before.labelled} → "
        f"{after.missed} of {after.labelled}, false alarms {before.false_alarms} → "
        f"{after.false_alarms}"
    )


def _amount(value: float, unit: str) -> str:
    return f"${value:.2f}" if unit == "$" else f"{value:.0f}s"


def _triage(
    session: Session, start: datetime, end: datetime, cutoff: datetime
) -> Measure:
    said = Finding.ai_enrichment["ai_triage"]["result"]["verdict"].astext
    stamp = Finding.ai_enrichment["ai_triage"]["timestamp"].astext
    # Guarded: a stamp that is not an ISO time is left out, not a failed cast.
    said_at = case((stamp.op("~")(_ISO), cast(stamp, DateTime)), else_=None)
    where = (
        # A standing verdict is never older than the first; this bounds the scan.
        Finding.first_verdict_at < _naive(end),
        said_at >= _naive(start),
        said_at < _naive(end),
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
    sample = exists().where(
        case_findings.c.case_id == Investigation.case_id,
        case_findings.c.finding_id == Finding.finding_id,
        Finding.data_source.in_(_NOT_REAL_DATA),
    )
    runs = (
        select(
            WorkflowRun.run_id,
            WorkflowRun.outcome,
            WorkflowRun.capped,
            WorkflowRun.finished_at,
            WorkflowRun.total_cost_usd,
            WorkflowRun.duration_ms,
            Investigation.case_id,
            # A Case's last run in the window is the one judged: a retry after a
            # failure replaces the failed run.
            func.row_number()
            .over(
                partition_by=func.coalesce(Investigation.case_id, WorkflowRun.run_id),
                order_by=(WorkflowRun.finished_at.desc(), WorkflowRun.run_id.desc()),
            )
            .label("rank"),
        )
        .join(
            Investigation,
            Investigation.investigation_id
            == WorkflowRun.trigger_context["investigation_id"].astext,
        )
        .where(
            WorkflowRun.finished_at >= _naive(start),
            WorkflowRun.finished_at < _naive(end),
            WorkflowRun.deleted_at.is_(None),
            func.coalesce(WorkflowRun.trigger_context["run_kind"].astext, "")
            != _SHADOW_KIND,
            ~sample,
        )
        .subquery("followup_runs")
    )
    closure = CaseClosureInfo
    judged = and_(
        runs.c.rank == 1,
        closure.case_id.isnot(None),
        closure.closed_at >= runs.c.finished_at,
        closure.closed_at <= _naive(cutoff),
    )
    real = and_(judged, closure.closure_category.in_(_REAL))
    not_real = and_(judged, closure.closure_category.in_(_NOT_REAL))
    dismissed = or_(
        *(
            func.coalesce(Case.activities, func.jsonb_build_array()).op("@>")(
                func.jsonb_build_array(
                    func.jsonb_build_object(
                        "activity_type",
                        literal(kind),
                        "details",
                        func.jsonb_build_object("run_id", runs.c.run_id),
                    )
                )
            )
            for kind in _DISMISSED
        )
    )
    # A null outcome (the console ended the run) or a turn cap is not completed.
    not_completed = or_(
        runs.c.outcome.is_distinct_from("completed"), runs.c.capped.is_(True)
    )
    row = session.execute(
        select(
            func.count().filter(or_(real, not_real)),
            func.count().filter(and_(real, or_(not_completed, dismissed))),
            func.count().filter(and_(not_real, ~not_completed, ~dismissed)),
            func.avg(runs.c.total_cost_usd),
            func.avg(runs.c.duration_ms),
        )
        .select_from(runs)
        .outerjoin(Case, Case.case_id == runs.c.case_id)
        .outerjoin(
            closure,
            and_(
                closure.case_id == runs.c.case_id,
                closure.close_channel.in_(PERSON_CHANNELS),
            ),
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
