"""Why investigations stopped early, by reason, and how their Cases were closed after.

Read-only. Counted per run: a stop is one of an investigation's runs that ended
without a full answer, in the window by the time it ended, so an investigation
retried after a stop can count more than once:

- the budget refused another iteration: the call limit, the cost cap or the
  runtime cap, which the agent layer tells apart only in the run's ``reason``;
- the AI gateway refused the call because its spending limit is used up;
- any other budget stop, whose reason does not name the limit: a hunt that
  parked at a limit and that an operator then concluded, or a trace the pool cut
  off;
- a model call failed, by the cause the agent layer named;
- it was abandoned: picked up again and again without advancing, or parked on a
  question nobody answered;
- it had no data to look at (``data_starved``);
- it failed for any other reason, or the console ended it before the agent layer
  did;
- it ran out of turns: a completed report the turn cap ended.

``budget_exhausted`` and ``data_starved`` are stored as completed, so the reason
is read from ``outcome``, never ``status``. An operator's stop (aborted, cancelled)
is a person's decision, not a failure, and is left out.

An investigation Vigil killed for no activity has no run outcome of its own: it
is counted once, from the investigation, and its runs are not counted again
under another reason. It is windowed by its last activity, which is up to the
stale threshold before the kill.

Each reason also says how many of those stops a person later closed as real, and
which tool failed most in their runs. Investigations of sample alerts and probes
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
    "budget": "Hit a budget limit",
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
    runs = _stopped_runs(start, end)
    quiet = _quiet(start, end)
    stops = union_all(
        select(runs.c.reason, runs.c.real), select(quiet.c.reason, quiet.c.real)
    ).subquery("stops")
    with unit_of_work() as session:
        counts = session.execute(
            select(
                stops.c.reason, func.count(), func.count().filter(stops.c.real)
            ).group_by(stops.c.reason)
        ).all()
        tools = _top_tools(session, runs, quiet)
    items = [
        {"reason": LABELS[key], "count": n, "note": _note(real, tools.get(key))}
        for key, n, real in counts
    ]
    items.sort(key=lambda item: (-item["count"], item["reason"]))
    return {"days": days, "items": items}


def _stopped_runs(start: datetime, end: datetime):
    """One row per stopped run: its reason, whether its Case was later closed as real."""
    runs = (
        select(
            _run_reason().label("reason"),
            _closed_real(Investigation.case_id, WorkflowRun.finished_at).label("real"),
            WorkflowRun.run_id.label("run_id"),
        )
        .join(Investigation, Investigation.investigation_id == _investigation_of_run())
        .where(
            WorkflowRun.finished_at >= start,
            WorkflowRun.finished_at < end,
            *_counted_run(),
            Investigation.master_review_notes.is_distinct_from(STALE_NOTE),
            ~_not_real_data(),
        )
        .subquery()
    )
    return select(runs).where(runs.c.reason.isnot(None)).subquery("stopped_runs")


def _quiet(start: datetime, end: datetime):
    """One row per investigation killed for going quiet, windowed by its last activity."""
    return (
        select(
            literal("stale").label("reason"),
            _closed_real(Investigation.case_id, Investigation.last_activity_at).label(
                "real"
            ),
            Investigation.investigation_id.label("investigation_id"),
        )
        .where(
            Investigation.master_review_notes == STALE_NOTE,
            Investigation.status == "failed",
            Investigation.last_activity_at >= start,
            Investigation.last_activity_at < end,
            ~_not_real_data(),
        )
        .subquery("quiet")
    )


def _investigation_of_run():
    return WorkflowRun.trigger_context["investigation_id"].astext


def _counted_run():
    """A run still in History that is the investigation's own, not its shadow."""
    return (
        WorkflowRun.deleted_at.is_(None),
        func.coalesce(WorkflowRun.trigger_context["run_kind"].astext, "")
        != _SHADOW_KIND,
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
        (and_(budget, reason.contains("calls_exhausted", autoescape=True)), "calls"),
        (and_(budget, reason.contains("cost_exhausted", autoescape=True)), "cost"),
        (and_(budget, reason.contains("wall_exhausted", autoescape=True)), "wall"),
        (and_(or_(budget, failed), cause == "gateway_budget_refused"), "gateway"),
        (and_(failed, cause.in_(_MODEL_CAUSES)), cause),
        (and_(failed, cause.isnot(None)), "model_error"),
        (
            and_(
                outcome == "abandoned", reason.startswith("picked up ", autoescape=True)
            ),
            "stalled",
        ),
        (outcome == "abandoned", "unanswered"),
        (outcome == "data_starved", "no_data"),
        (budget, "budget"),
        (failed, "other"),
        (and_(outcome == "completed", WorkflowRun.capped.is_(True)), "turns"),
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
    in_case = (
        exists()
        .where(
            case_findings.c.case_id == Investigation.case_id,
            case_findings.c.finding_id == Finding.finding_id,
            Finding.data_source.in_(_NOT_REAL_DATA),
        )
        .correlate(Investigation)
    )
    # Looked up by id from the investigation's own list, so the findings table is
    # read by its key rather than scanned.
    trigger_ids = select(
        func.jsonb_array_elements_text(Investigation.trigger_ids)
    ).correlate(Investigation)
    started_by = (
        exists()
        .where(
            Finding.finding_id.in_(trigger_ids),
            Finding.data_source.in_(_NOT_REAL_DATA),
        )
        .correlate(Investigation)
    )
    return or_(in_case, started_by)


def _top_tools(session: Session, runs, quiet) -> dict[str, tuple[str, int]]:
    """Per reason, the tool whose calls failed most in its stopped runs."""
    # A quiet investigation's runs are all of its counted runs; any other stop is one run.
    stale_runs = (
        select(quiet.c.reason, WorkflowRun.run_id)
        .join(WorkflowRun, _investigation_of_run() == quiet.c.investigation_id)
        .where(*_counted_run())
    )
    ledger = union_all(select(runs.c.reason, runs.c.run_id), stale_runs).subquery(
        "ledger_runs"
    )
    # Guarded: a console run id that is not a uuid has no ledger.
    ledger_id = case(
        (
            ledger.c.run_id.op("~*")(_UUID),
            cast(ledger.c.run_id, UUID(as_uuid=False)),
        ),
        else_=None,
    )
    tool = _agent_events.c.payload["tool"].astext
    rows = session.execute(
        select(ledger.c.reason, tool, func.count())
        .select_from(ledger)
        .join(_agent_events, _agent_events.c.run_id == ledger_id)
        .where(
            _agent_events.c.kind == "call",
            _agent_events.c.payload["ok"].astext == "false",
        )
        .group_by(ledger.c.reason, tool)
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
