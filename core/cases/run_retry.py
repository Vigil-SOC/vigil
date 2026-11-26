"""Running a case's investigation again when it failed for a reason that passes.

The agent worker retries a model call for a few seconds (core/llm/gateway_retry.py);
an outage longer than that fails the whole run, and nothing else would run the
case again. The orchestrator asks ``retries_due`` on its tick and launches the
same workflow on the same findings through its ordinary launch path.

Only the orchestrator's own investigations are retried: a run started from the
console was started by a person, who sees it fail. Only a failure that passes is
retried: a refused call or a bad spec fails the same way every time. The agent
types a failed model call in ``workflow_runs.stop_cause`` (STOP_CAUSES in
services/agent/contracts/events.ts), and that cause decides alone; a failure
outside the model call has none, and its message is read instead for a passing
cause (a provider 5xx, a timeout, a dropped connection).
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Dict, List, Optional

from core.time import utcnow

logger = logging.getLogger(__name__)

RETRY_ACTIVITY = "run_retried"

# How far back a failure is still worth retrying: a case that failed days ago
# and has sat since is an analyst's to re-run, not a surprise launch.
RETRY_WINDOW = timedelta(hours=24)

# A case nobody is working: closed, or parked until someone returns to it.
IDLE_CASE_STATUSES = ("closed", "parked")

_PASSING_CODES = r"(?:408|429|500|502|503|504|529)"
# A status code counts only where it reads as one ("HTTP 503", "Error code: 529",
# "502 Bad Gateway"), not as any number in the text ("limit of 500 results").
TRANSIENT = re.compile(
    rf"\b(?:HTTP\S*|status|code|error)[\s:=]*{_PASSING_CODES}\b"
    rf"|\b{_PASSING_CODES}\s+(?:Request Timeout|Too Many Requests"
    r"|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Timeout)"
    r"|timed? ?out|timeout|overloaded|rate.?limit"
    r"|(?:service|temporarily) unavailable"
    r"|ECONNRESET|ECONNREFUSED|ETIMEDOUT|socket hang up|fetch failed"
    r"|failed to execute HTTP request|connection (reset|refused|error|closed)",
    re.IGNORECASE,
)


# Typed causes that pass. provider_auth_refused, gateway_budget_refused,
# output_truncated and unknown do not, whatever their message says.
PASSING_STOP_CAUSES = frozenset({"rate_limited", "model_unavailable"})


def transient(reason: Optional[str]) -> bool:
    """True when a run's failure message names a cause that passes."""
    return reason is not None and TRANSIENT.search(reason) is not None


def passes(stop_cause: Optional[str], reason: Optional[str]) -> bool:
    """True when a failed run failed for a cause that passes: its typed
    stop_cause when it has one, else its message."""
    if stop_cause:
        return stop_cause in PASSING_STOP_CAUSES
    return transient(reason)


def retries_due(session, after: timedelta, attempts: int) -> List[Dict[str, Any]]:
    """Cases whose newest run is a failed orchestrator investigation, failed for
    a passing reason between ``RETRY_WINDOW`` and ``after`` ago, with no run
    live and no more than ``attempts`` failures in a row. Each: case_id, the
    failed run and its investigation, workflow, findings and priority."""
    from sqlalchemy import select

    from core.storage.models import Case, Investigation, WorkflowRun
    from core.workflows.workflow_run_service import IN_FLIGHT_STATUSES

    if attempts <= 0:
        return []
    now = utcnow()
    case_of = WorkflowRun.trigger_context["case_id"].astext
    failed = session.execute(
        select(WorkflowRun)
        .where(
            WorkflowRun.status == "failed",
            WorkflowRun.deleted_at.is_(None),
            WorkflowRun.triggered_by == "orchestrator",
            case_of.isnot(None),
            WorkflowRun.trigger_context["investigation_id"].astext.isnot(None),
            WorkflowRun.finished_at.isnot(None),
            WorkflowRun.finished_at <= now - after,
            WorkflowRun.finished_at >= now - RETRY_WINDOW,
        )
        .order_by(WorkflowRun.started_at.desc())
    ).scalars()
    due: List[Dict[str, Any]] = []
    seen = set()
    for run in failed:
        case_id = run.trigger_context["case_id"]
        if case_id in seen:
            continue
        seen.add(case_id)
        if not passes(run.stop_cause, run.error or run.reason):
            continue
        case = session.get(Case, case_id)
        if case is None or case.status in IDLE_CASE_STATUSES:
            continue
        runs = session.execute(
            select(WorkflowRun.status, WorkflowRun.started_at)
            .where(case_of == case_id, WorkflowRun.deleted_at.is_(None))
            .order_by(WorkflowRun.started_at.desc())
        ).all()
        # Anything live, or anything newer than this failure, means the case has
        # moved on without it.
        if any(status in IN_FLIGHT_STATUSES for status, _ in runs):
            continue
        if runs and runs[0][1] > run.started_at:
            continue
        if _failures_in_a_row([status for status, _ in runs]) > attempts:
            continue
        # An investigation opened since, with or without a run, is the case
        # already being worked again.
        if session.execute(
            select(Investigation.investigation_id)
            .where(
                Investigation.case_id == case_id,
                Investigation.created_at > run.finished_at,
            )
            .limit(1)
        ).first():
            continue
        investigation = session.get(
            Investigation, run.trigger_context["investigation_id"]
        )
        if (
            investigation is None
            or investigation.trigger_type != "finding"
            or not investigation.trigger_ids
        ):
            continue
        due.append(
            {
                "case_id": case_id,
                "run_id": run.run_id,
                "investigation_id": investigation.investigation_id,
                "workflow_id": investigation.workflow_id,
                "finding_ids": list(investigation.trigger_ids),
                "priority": investigation.priority,
                "reason": run.error or run.reason,
                "stop_cause": run.stop_cause,
            }
        )
    return due


def _failures_in_a_row(statuses: List[str]) -> int:
    """How many of a case's latest runs failed, newest first, before one that did
    not. A retry that fails too adds one, so this counts the retries."""
    count = 0
    for status in statuses:
        if status != "failed":
            break
        count += 1
    return count


def note_retry(session, item: Dict[str, Any]) -> None:
    """Record on the case that its investigation was run again, and why."""
    from core.agents.builtins import ORCHESTRATOR_ACTOR
    from core.cases.agent_closure import activity_actor
    from core.storage.models import Case

    case = session.get(Case, item["case_id"])
    if case is None:
        return
    case.activities = [
        *(case.activities or []),
        {
            "timestamp": utcnow().isoformat() + "Z",
            "activity_type": RETRY_ACTIVITY,
            "description": "The investigation failed for a passing reason"
            + (f" ({item['stop_cause']})" if item.get("stop_cause") else "")
            + f" and was run again: {str(item.get('reason') or '')[:200]}",
            "details": {
                "failed_run_id": item["run_id"],
                "failed_investigation_id": item["investigation_id"],
                "stop_cause": item.get("stop_cause"),
            },
            **activity_actor(ORCHESTRATOR_ACTOR, session),
        },
    ]


__all__ = ["RETRY_ACTIVITY", "note_retry", "passes", "retries_due", "transient"]
