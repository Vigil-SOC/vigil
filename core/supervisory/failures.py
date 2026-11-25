"""Why agent runs stopped early, by run kind and reason, and how their Cases were
closed after.

Read-only. Every agent-run kind that investigates is counted: investigations,
threat hunts, root-cause traces and workflows (``investigate``, ``hunt``,
``root_cause``, ``compose``). ``adjudicate``, a second look at another run's
verdict, is not. Each reason is counted per kind and named with it, so a hunt's
budget stop never shares a line with an investigation's; the one exception is the
quiet kill below, which is the orchestrator's and carries no kind.

Counted per run: a stop is a run that ended without a full answer, in the window
by the time it ended, so a run retried after a stop can count more than once:

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
- it ran out of turns: a completed report the turn cap ended, read where the
  run reports the cap;
- it was a hunt that parked and nobody decided on before the park expired.

A hunt out of turns or budget with a hypothesis still open parks rather than
ending, so it is counted only once it ends.

``budget_exhausted`` and ``data_starved`` are stored as completed, so the reason
is read from ``outcome``, never ``status``. An operator's stop (aborted, cancelled)
is a person's decision, not a failure, and is left out; an expired park is stored
as aborted too, and is told apart by its reason.

An investigation Vigil killed for no activity has no run outcome of its own: it
is counted once, from the investigation, and its runs are not counted again
under another reason. It is windowed by its last activity, which is up to the
stale threshold before the kill.

Each reason also says, of its stops that have a Case, how many a person later
closed as real, and which tool failed most in their runs. A run's Case is its
investigation's, else the Case it was started on; a run with neither (a
scheduled hunt, say) has nothing a person's close could judge, so it is counted
as a stop but not in that note. Runs on sample alerts and probes are left out,
through their Case or the findings that started their investigation.
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
# The agent-run kinds that investigate. adjudicate, a second look at another
# run's verdict, is not one of them.
KINDS = ("investigate", "hunt", "root_cause", "compose")
KIND_LABELS = {
    "investigate": "Investigations",
    "hunt": "Threat hunts",
    "root_cause": "Root-cause traces",
    "compose": "Workflows",
}
# A run that ends in a window can have started any time before it: a hunt can
# park for up to 30 days, be extended and park again, with its start time set
# once. So runs are read by when they ended, and the start time bounds them only
# from above.
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
    "parked": "Parked, then timed out with no decision",
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
    """Early stops in the window by run kind and reason, most first."""
    end = _naive(now or datetime.now(UTC))
    start = end - timedelta(days=days)
    runs = _stopped_runs(start, end)
    quiet = _quiet(start, end)
    stops = union_all(
        select(runs.c.kind, runs.c.reason, runs.c.has_case, runs.c.real),
        select(quiet.c.kind, quiet.c.reason, quiet.c.has_case, quiet.c.real),
    ).subquery("stops")
    with unit_of_work() as session:
        counts = session.execute(
            select(
                stops.c.kind,
                stops.c.reason,
                func.count(),
                func.count().filter(stops.c.has_case),
                func.count().filter(stops.c.real),
            ).group_by(stops.c.kind, stops.c.reason)
        ).all()
        tools = _top_tools(session, runs, quiet, end)
    items = [
        {
            "reason": _label(kind, key),
            "count": n,
            "note": _note(with_case, real, tools.get((kind, key))),
        }
        for kind, key, n, with_case, real in counts
    ]
    items.sort(key=lambda item: (-item["count"], item["reason"]))
    return {"days": days, "items": items}


def _stopped_runs(start: datetime, end: datetime):
    """One row per stopped run: its kind and reason, whether it has a Case, and
    whether that Case was later closed as real."""
    case_id = _case_of_run()
    runs = (
        select(
            _kind().label("kind"),
            _run_reason().label("reason"),
            case_id.isnot(None).label("has_case"),
            _closed_real(case_id, WorkflowRun.finished_at).label("real"),
            WorkflowRun.run_id.label("run_id"),
        )
        .outerjoin(
            Investigation, Investigation.investigation_id == _investigation_of_run()
        )
        .where(
            WorkflowRun.started_at < end,
            WorkflowRun.finished_at >= start,
            WorkflowRun.finished_at < end,
            *_counted_run(),
            Investigation.master_review_notes.is_distinct_from(STALE_NOTE),
            ~_not_real_data(case_id),
        )
        .subquery()
    )
    return select(runs).where(runs.c.reason.isnot(None)).subquery("stopped_runs")


def _quiet(start: datetime, end: datetime):
    """One row per investigation killed for going quiet, windowed by its last activity."""
    return (
        select(
            # The orchestrator's kill is on the investigation, not on a run kind.
            literal(None, String).label("kind"),
            literal("stale").label("reason"),
            func.nullif(Investigation.case_id, "").isnot(None).label("has_case"),
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
            ~_not_real_data(Investigation.case_id),
        )
        .subquery("quiet")
    )


def _investigation_of_run():
    return WorkflowRun.trigger_context["investigation_id"].astext


def _kind():
    return WorkflowRun.trigger_context["run_kind"].astext


def _case_of_run():
    """The run's Case: its investigation's, else the one the run was started on."""
    return func.coalesce(
        func.nullif(Investigation.case_id, ""),
        func.nullif(WorkflowRun.trigger_context["case_id"].astext, ""),
    )


def _counted_run():
    """A run still in History, of a kind that investigates (so not a shadow)."""
    return (WorkflowRun.deleted_at.is_(None), _kind().in_(KINDS))


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
        # A hunt parked past its TTL ends aborted, though no person stopped it.
        (
            and_(
                outcome == "aborted",
                reason.startswith("parked since ", autoescape=True),
            ),
            "parked",
        ),
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


def _not_real_data(case_id):
    """The run's Case, or a finding that started its investigation, is a sample or
    probe. A run with neither has no link to sample data, and counts."""
    in_case = (
        exists().where(
            case_findings.c.case_id == case_id,
            case_findings.c.finding_id == Finding.finding_id,
            Finding.data_source.in_(_NOT_REAL_DATA),
        )
        # The run's Case can come from the run itself, so both outer tables correlate.
        .correlate_except(case_findings, Finding)
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


def _top_tools(
    session: Session, runs, quiet, end: datetime
) -> dict[str, tuple[str, int]]:
    """Per reason, the tool whose calls failed most in its stopped runs."""
    # A quiet investigation's runs are all of its counted runs; any other stop is one run.
    stale_runs = (
        select(quiet.c.kind, quiet.c.reason, WorkflowRun.run_id)
        .join(WorkflowRun, _investigation_of_run() == quiet.c.investigation_id)
        .where(*_counted_run(), WorkflowRun.started_at < end)
    )
    ledger = union_all(
        select(runs.c.kind, runs.c.reason, runs.c.run_id), stale_runs
    ).subquery("ledger_runs")
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
        select(ledger.c.kind, ledger.c.reason, tool, func.count())
        .select_from(ledger)
        .join(_agent_events, _agent_events.c.run_id == ledger_id)
        .where(
            _agent_events.c.kind == "call",
            _agent_events.c.payload["ok"].astext == "false",
        )
        .group_by(ledger.c.kind, ledger.c.reason, tool)
    ).all()
    top: dict[tuple, tuple[str, int]] = {}
    for kind, reason, name, n in rows:
        key = (kind, reason)
        if name and (key not in top or (-n, name) < (-top[key][1], top[key][0])):
            top[key] = (name, n)
    return top


def _label(kind: str | None, reason: str) -> str:
    """The reason, under the run kind it stopped, so a hunt's stop and an
    investigation's never share a line."""
    return f"{KIND_LABELS[kind]} · {LABELS[reason]}" if kind else LABELS[reason]


def _note(with_case: int, real: int, tool: tuple[str, int] | None) -> str | None:
    """How many with a Case a person later closed as real, and the tool that failed
    most. A run with no Case has nothing to be judged by, so it is not counted."""
    parts = []
    if with_case:
        parts.append(f"{real} of {with_case} with a Case later closed as real")
    if tool is not None:
        name, n = tool
        parts.append(f"{name} failed most ({n} {'error' if n == 1 else 'errors'})")
    return " · ".join(parts) or None
