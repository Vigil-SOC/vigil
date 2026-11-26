"""What the supervisory model has to learn from: people's judgements, per function.

Read-only. A label is a person's judgement, never an agent's:

- triage: a finding first triaged in the window whose winning determination (a
  closed Case or a noise mark) is a person's;
- investigation: a Case an agent run worked on (an investigation, a hunt, a
  root-cause trace or a composed workflow), closed in the window by a person with
  a category that says real or not real. A shadow adjudication is not a run on
  the Case;
- response: an action Vigil proposed that a person approved or rejected in the
  window. A person's own proposal, and the row a person's undo writes, are not
  judgements on Vigil and are left out.

Sample alerts and probes are left out, as the production tuning read does: a Case
counts only when none of its findings is one. Approval rows carry no data source;
probes never propose an action.

Each function is windowed by its own label date (first verdict, close, decision).
``learns_from`` counts the raw feedback by the date it was given, so its numbers
are where labels come from, not a sum of the function counts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session, aliased

from core.cases.closure import (
    PERSON_CHANNELS,
    ClosedByKind,
    ClosureCategory,
    closure_says_real,
)
from core.detections.inventory.accurate import MIN_JUDGED, _closes, verdict_of
from core.ingestion.sample_alerts import SAMPLE_DATA_SOURCE
from core.storage.models import Finding
from core.storage.models.auth import Principal
from core.storage.models.base import case_findings
from core.storage.models.case_entities import CaseClosureInfo
from core.storage.models.workflow import ApprovalAction, Investigation, WorkflowRun
from core.storage.unit_of_work import unit_of_work
from core.workflows.workflows_service import (
    COMPOSE_RUN_KIND,
    HUNT_RUN_KIND,
    INVESTIGATE_RUN_KIND,
    ROOT_CAUSE_RUN_KIND,
)

# The same floor the detection Accurate check and production tuning use.
FLOOR = MIN_JUDGED
WINDOW_DAYS = 30
# core cannot import services.daemon.probes. The probe source is this string.
_PROBE = "probe"
_NOT_REAL_DATA = (SAMPLE_DATA_SOURCE, _PROBE)
# A decision a person made: the row left pending either way.
_DECIDED = ("approved", "rejected", "executed", "failed")
# The agent runs a person's close of their Case judges. All share one model setting.
_RUN_KINDS = (
    INVESTIGATE_RUN_KIND,
    HUNT_RUN_KIND,
    ROOT_CAUSE_RUN_KIND,
    COMPOSE_RUN_KIND,
)
# Close categories that say real or not real; the rest determined neither.
_DETERMINED = tuple(
    c.value for c in ClosureCategory if closure_says_real(c.value) is not None
)

UNITS = {
    "triage": "labelled findings",
    "investigation": "closed cases",
    "response": "approval decisions",
}


def feedback(now: datetime | None = None, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Per-function label counts against the floor, and what they come from."""
    end = _naive(now or datetime.now(UTC))
    start = end - timedelta(days=days)
    with unit_of_work() as session:
        counts = {
            "triage": _triage(session, start, end),
            "investigation": _investigation(session, start, end),
            "response": _response(session, start, end),
        }
        closed = _person_closes(session, start, end)
        noise = _analyst_noise(session, start, end)
    return {
        "history_days": days,
        "functions": [
            {
                "fn": fn,
                "state": "ready" if n >= FLOOR else "waiting",
                "labelled": n,
                "floor": FLOOR,
                "unit": UNITS[fn],
            }
            for fn, n in counts.items()
        ],
        "learns_from": [
            {"label": "Cases closed with a category", "count": closed},
            {"label": "Marked as noise by analysts", "count": noise},
            {"label": "Approval decisions", "count": counts["response"]},
        ],
    }


def _triage(session: Session, start: datetime, end: datetime) -> int:
    """Findings first triaged in the window that a person judged."""
    where = (
        Finding.first_verdict_at >= start,
        Finding.first_verdict_at < end,
        Finding.data_source.notin_(_NOT_REAL_DATA),
        # Only a noise mark or a person's close can make a person's determination.
        or_(Finding.noise_marked_at.isnot(None), _in_person_closed_case()),
    )
    closes = _closes(session, *where, sources=None)
    rows = session.execute(
        select(
            Finding.finding_id, Finding.noise_marked_at, Finding.noise_marked_by_kind
        ).where(*where)
    )
    judged = 0
    for fid, marked, kind in rows:
        won = verdict_of(closes.get(fid, []), marked, kind)
        if won is not None and won[1]:
            judged += 1
    return judged


def _in_person_closed_case():
    """A finding sits in a Case a person closed. Aliased: callers join these tables too."""
    link = case_findings.alias("judged_link")
    closure = aliased(CaseClosureInfo, name="judged_closure")
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


def _investigation(session: Session, start: datetime, end: datetime) -> int:
    """Cases an agent run worked on, closed by a person in the window, real or not.

    A daemon investigation is tied to its Case by the investigation row; any other
    run by the ``case_id`` its trigger carries."""
    investigated = exists().where(Investigation.case_id == CaseClosureInfo.case_id)
    run_on = exists().where(
        WorkflowRun.trigger_context["case_id"].astext == CaseClosureInfo.case_id,
        WorkflowRun.trigger_context["run_kind"].astext.in_(_RUN_KINDS),
        WorkflowRun.deleted_at.is_(None),
    )
    return _count_closes(session, start, end, or_(investigated, run_on))


def _response(session: Session, start: datetime, end: datetime) -> int:
    """Actions Vigil proposed that a person approved or rejected in the window."""
    proposer = aliased(Principal)
    person_proposed = exists().where(
        proposer.principal_id == ApprovalAction.created_by_principal_id,
        proposer.kind == "person",
    )
    return session.execute(
        select(func.count(ApprovalAction.action_id))
        .join(
            Principal,
            and_(
                Principal.principal_id == ApprovalAction.approved_by_principal_id,
                Principal.kind == "person",
            ),
        )
        .where(
            ApprovalAction.status.in_(_DECIDED),
            ApprovalAction.approved_at >= start,
            ApprovalAction.approved_at < end,
            ApprovalAction.parameters["undoes"].astext.is_(None),
            ~person_proposed,
        )
    ).scalar_one()


def _person_closes(session: Session, start: datetime, end: datetime) -> int:
    """Every Case a person closed in the window with a category that says real or not."""
    return _count_closes(session, start, end)


def _count_closes(session: Session, start: datetime, end: datetime, *extra) -> int:
    """Real-data Cases a person closed in the window, real or not, under ``extra``."""
    sample = exists().where(
        case_findings.c.case_id == CaseClosureInfo.case_id,
        case_findings.c.finding_id == Finding.finding_id,
        Finding.data_source.in_(_NOT_REAL_DATA),
    )
    return session.execute(
        select(func.count(CaseClosureInfo.case_id)).where(
            CaseClosureInfo.closed_at >= start,
            CaseClosureInfo.closed_at < end,
            CaseClosureInfo.close_channel.in_(PERSON_CHANNELS),
            CaseClosureInfo.closure_category.in_(_DETERMINED),
            ~sample,
            *extra,
        )
    ).scalar_one()


def _analyst_noise(session: Session, start: datetime, end: datetime) -> int:
    """Findings a person marked as noise in the window."""
    return session.execute(
        select(func.count(Finding.finding_id)).where(
            Finding.noise_marked_at >= start,
            Finding.noise_marked_at < end,
            Finding.noise_marked_by_kind == ClosedByKind.ANALYST.value,
            Finding.data_source.notin_(_NOT_REAL_DATA),
        )
    ).scalar_one()


def _naive(value: datetime) -> datetime:
    """UTC without tzinfo, as the columns store it."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
