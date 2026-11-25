"""Why investigations stopped early, by reason, and how their Cases were closed after.

Read-only. A stop is an investigation's run that ended without a full answer, in
the window by the time it ended:

- the budget refused another iteration: the call limit, the cost cap or the
  runtime cap, which the agent layer tells apart only in the run's ``reason``;
- the AI gateway refused the call because its spending limit is used up;
- a model call failed, by the cause the agent layer named;
- it was abandoned: picked up again and again without advancing, or parked on a
  question nobody answered;
- it had no data to look at (``data_starved``);
- it failed for any other reason, or the console ended it before the agent layer
  did;
- it ran out of turns: a report the turn cap ended.

``budget_exhausted`` and ``data_starved`` are stored as completed, so the reason
is read from ``outcome``, never ``status``. An operator's stop (aborted, cancelled)
is a person's decision, not a failure, and is left out.

An investigation Vigil killed for no activity has no run outcome of its own: it
is counted from the investigation, windowed by its last activity, and its runs
are not counted again under another reason.

Each reason also says how many of those stops a person later closed as real, and
which tool failed most in those runs. Investigations of sample alerts and probes
are left out, through their Case or the findings that started them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    column,
    exists,
    func,
    literal,
    or_,
    select,
    table,
    union_all,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Session

from core.cases.closure import PERSON_CHANNELS, ClosureCategory, closure_says_real
from core.storage.models import Finding
from core.storage.models.base import case_findings
from core.storage.models.case_entities import CaseClosureInfo
from core.storage.models.workflow import Investigation, WorkflowRun
from core.storage.unit_of_work import unit_of_work
from core.supervisory.feedback import _NOT_REAL_DATA, WINDOW_DAYS, _naive

# What the orchestrator writes when it kills an investigation that went quiet.
STALE_NOTE = "Stale: no activity"
# The orchestrator's second opinion on an investigation, not the investigation.
_SHADOW_KIND = "adjudicate"
_REAL = tuple(c.value for c in ClosureCategory if closure_says_real(c.value) is True)
_UUID = "^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$"

LABELS = {
    "turns": "Ran out of turns",
    "calls": "Hit the call limit",
    "cost": "Hit the cost cap",
    "wall": "Hit the runtime cap",
    "gateway": "AI gateway spending limit used up",
    "rate_limited": "Model provider rate limited it",
    "provider_auth_refused": "Model provider refused the key",
    "model_unavailable": "Model did not answer",
    "output_truncated": "Model output was cut off",
    "model_error": "A model call failed",
    "stalled": "Stopped advancing",
    "unanswered": "Its question went unanswered",
    "no_data": "No data to look at",
    "stale": "Went quiet and was stopped",
    "other": "Failed for another reason",
}
_MODEL_CAUSES = (
    "rate_limited",
    "provider_auth_refused",
    "model_unavailable",
    "output_truncated",
)

_agent_events = table(
    "agent_events",
    column("run_id", UUID(as_uuid=False)),
    column("kind", String),
    column("payload", JSONB),
)


def failures(now: datetime | None = None, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Early stops in the window by reason, most first."""
    end = _naive(now or datetime.now(UTC))
    start = end - timedelta(days=days)
    with unit_of_work() as session:
        stops = _stops(start, end)
        counts = session.execute(
            select(
                stops.c.reason, func.count(), func.count().filter(stops.c.real)
            ).group_by(stops.c.reason)
        ).all()
        tools = _top_tools(session, stops)
    items = [
        {"reason": LABELS[key], "count": n, "note": _note(real, tools.get(key))}
        for key, n, real in counts
    ]
    items.sort(key=lambda item: (-item["count"], item["reason"]))
    return {"days": days, "items": items}


def _stops(start: datetime, end: datetime):
    """One row per stop: its reason, whether its Case was later closed as real, its runs."""
    inv_id = WorkflowRun.trigger_context["investigation_id"].astext
    stale = Investigation.master_review_notes == STALE_NOTE
    runs = (
        select(
            _run_reason().label("reason"),
            _closed_real(Investigation.case_id, WorkflowRun.finished_at).label("real"),
            Investigation.investigation_id.label("investigation_id"),
            WorkflowRun.run_id.label("run_id"),
        )
        .join(Investigation, Investigation.investigation_id == inv_id)
        .where(
            WorkflowRun.finished_at >= start,
            WorkflowRun.finished_at < end,
            WorkflowRun.deleted_at.is_(None),
            func.coalesce(WorkflowRun.trigger_context["run_kind"].astext, "")
            != _SHADOW_KIND,
            Investigation.master_review_notes.is_distinct_from(STALE_NOTE),
            ~_not_real_data(),
        )
        .subquery()
    )
    quiet = select(
        literal("stale").label("reason"),
        _closed_real(Investigation.case_id, Investigation.last_activity_at).label(
            "real"
        ),
        Investigation.investigation_id.label("investigation_id"),
        literal(None, String).label("run_id"),
    ).where(
        stale,
        Investigation.status == "failed",
        Investigation.last_activity_at >= start,
        Investigation.last_activity_at < end,
        ~_not_real_data(),
    )
    return union_all(select(runs).where(runs.c.reason.isnot(None)), quiet).subquery(
        "stops"
    )


def _run_reason():
    """Why a run stopped early, or None for a run that did not."""
    outcome, reason, cause = (
        WorkflowRun.outcome,
        WorkflowRun.reason,
        WorkflowRun.stop_cause,
    )
    budget = outcome == "budget_exhausted"
    failed = or_(
        outcome == "failed", and_(outcome.is_(None), WorkflowRun.status == "failed")
    )
    return case(
        (and_(budget, reason.contains("calls_exhausted")), "calls"),
        (and_(budget, reason.contains("cost_exhausted")), "cost"),
        (and_(budget, reason.contains("wall_exhausted")), "wall"),
        (cause == "gateway_budget_refused", "gateway"),
        (and_(failed, cause.in_(_MODEL_CAUSES)), cause),
        (and_(failed, cause.isnot(None)), "model_error"),
        (and_(outcome == "abandoned", reason.like("picked up %")), "stalled"),
        (outcome == "abandoned", "unanswered"),
        (outcome == "data_starved", "no_data"),
        (or_(failed, budget), "other"),
        (WorkflowRun.capped.is_(True), "turns"),
        else_=None,
    )


def _closed_real(case_id, after):
    """The Case was closed by a person as real at or after ``after``."""
    return exists().where(
        CaseClosureInfo.case_id == case_id,
        CaseClosureInfo.close_channel.in_(PERSON_CHANNELS),
        CaseClosureInfo.closure_category.in_(_REAL),
        CaseClosureInfo.closed_at >= after,
    )


def _not_real_data():
    """The investigation's Case, or a finding that started it, is a sample or probe."""
    in_case = exists().where(
        case_findings.c.case_id == Investigation.case_id,
        case_findings.c.finding_id == Finding.finding_id,
        Finding.data_source.in_(_NOT_REAL_DATA),
    )
    started_by = exists().where(
        Investigation.trigger_ids.op("?")(Finding.finding_id),
        Finding.data_source.in_(_NOT_REAL_DATA),
    )
    return or_(in_case, started_by)


def _top_tools(session: Session, stops) -> dict[str, tuple[str, int]]:
    """Per reason, the tool whose calls failed most in its stopped runs."""
    run = WorkflowRun.__table__.alias("stopped_run")
    # A stale stop's runs are every run of its investigation; any other is its own.
    own = and_(stops.c.run_id.isnot(None), run.c.run_id == stops.c.run_id)
    of_stale = and_(
        stops.c.run_id.is_(None),
        run.c.trigger_context["investigation_id"].astext == stops.c.investigation_id,
    )
    # Guarded: a console run id that is not a uuid has no ledger.
    ledger_id = case(
        (run.c.run_id.op("~*")(_UUID), cast(run.c.run_id, UUID(as_uuid=False))),
        else_=None,
    )
    tool = _agent_events.c.payload["tool"].astext
    rows = session.execute(
        select(stops.c.reason, tool, func.count())
        .select_from(stops)
        .join(run, or_(own, of_stale))
        .join(_agent_events, _agent_events.c.run_id == ledger_id)
        .where(
            _agent_events.c.kind == "call",
            _agent_events.c.payload["ok"].astext == "false",
        )
        .group_by(stops.c.reason, tool)
    ).all()
    top: dict[str, tuple[str, int]] = {}
    for reason, name, n in rows:
        if name and (
            reason not in top or (-n, name) < (-top[reason][1], top[reason][0])
        ):
            top[reason] = (name, n)
    return top


def _note(real: int, tool: tuple[str, int] | None) -> str:
    """How many were later closed as real, and the tool that failed most."""
    closed = f"{real} later closed as real" if real else "None later closed as real"
    if tool is None:
        return closed
    name, n = tool
    return f"{closed} · {name} failed most ({n} {'error' if n == 1 else 'errors'})"
