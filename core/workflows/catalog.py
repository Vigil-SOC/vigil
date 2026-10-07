"""The workflow catalog: what workflows exist, and what one of them is.

Two routers answer these reads and neither owns the answer. The frozen contract
serves them at ``/api/v1/workflows`` (``core/api/v1/workflows_router.py``); the
console serves the pre-version ``/api/workflows`` paths from its own router,
because ``/workflows/custom`` and ``/workflows/{workflow_id}`` are only
order-safe inside one router. So the reads live here, in the domain whose
language they speak, and both routers call down into them.

The detail read is the definition, plus the lead and model for a single-agent
workflow. Preflight (roles, model, skills, permissions, budgets, checkpoints) is
about executing a run, so it lives in ``core.workflows.hunt_preflight`` behind
its own console route.
"""

from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import func

from core.findings.overview import completion_level
from core.llm import target
from core.llm.providers.registry import get_registry, model_display_name
from core.storage.connection import get_db_manager
from core.storage.models import WorkflowRun
from core.time import utcnow
from core.workflows.enablement import disabled_workflow_ids
from core.workflows.routing import can_disable, triggers_for
from core.workflows.workflows_service import (
    ROOT_CAUSE_RUN_KIND,
    WorkflowsService,
    is_hunt_like,
)

# The run kinds driven by one lead agent, and the component its model is filed under.
SINGLE_AGENT_RUN_KINDS = frozenset({"investigate", ROOT_CAUSE_RUN_KIND})
INVESTIGATION_COMPONENT = "investigation"
LEAD_ROLE = "Lead analyst"


def is_hunt(workflows: WorkflowsService, workflow_id: Optional[str]) -> bool:
    """True when a workflow drives the hunt hypothesis loop.

    A hunt writes no phase rows: it has beliefs to report, not steps.
    """
    if not workflow_id:
        return False
    definition = workflows.get_workflow(str(workflow_id))
    return definition is not None and is_hunt_like(definition.run_kind)


def _week_run_stats(now: datetime) -> Dict[str, Dict[str, Any]]:
    """Per workflow, the runs started in the last 7 days and how they went.

    ``started_at`` is naive UTC, so the bound is too: an aware datetime
    TypeErrors against the column. ``runs_7d`` counts every run in the window,
    running ones included. ``success_rate`` is completed over those that ended
    (completed, failed, cancelled), null while none has. ``mean_cost_usd`` is the
    mean over runs with ``finished_at`` set; a finished run that cost nothing is
    a real zero, null is only "nothing finished". Deleted rows stay out of all.
    """
    since = now - timedelta(days=7)
    db = get_db_manager()
    with db.session_scope() as session:
        counts = (
            session.query(
                WorkflowRun.workflow_id,
                WorkflowRun.status,
                func.count(WorkflowRun.run_id),
            )
            .filter(WorkflowRun.started_at >= since, WorkflowRun.deleted_at.is_(None))
            .group_by(WorkflowRun.workflow_id, WorkflowRun.status)
            .all()
        )
        costs = (
            session.query(WorkflowRun.workflow_id, func.avg(WorkflowRun.total_cost_usd))
            .filter(
                WorkflowRun.started_at >= since,
                WorkflowRun.deleted_at.is_(None),
                WorkflowRun.finished_at.isnot(None),
            )
            .group_by(WorkflowRun.workflow_id)
            .all()
        )
    by_status: Dict[str, Dict[str, int]] = {}
    for workflow_id, status, count in counts:
        by_status.setdefault(str(workflow_id), {})[status] = int(count)
    mean = {
        str(workflow_id): float(avg) for workflow_id, avg in costs if avg is not None
    }
    stats = {}
    for workflow_id, statuses in by_status.items():
        completed = statuses.get("completed", 0)
        ended = completed + sum(statuses.get(s, 0) for s in ("failed", "cancelled"))
        stats[workflow_id] = {
            "runs_7d": sum(statuses.values()),
            "success_rate": completed / ended if ended else None,
            "success_level": completion_level(ended, completed),
            "mean_cost_usd": mean.get(workflow_id),
        }
    return stats


_NO_RUNS: Dict[str, Any] = {
    "runs_7d": 0,
    "success_rate": None,
    "success_level": None,
    "mean_cost_usd": None,
}


def listing(service: WorkflowsService) -> Dict[str, Any]:
    """Every available workflow, file-based and database-backed alike.

    Each row carries its last 7 days: ``runs_7d``, ``success_rate`` (a 0..1
    fraction), ``success_level`` (good/fair/poor, as on the Agents tab) and the
    mean cost of the finished runs. All four keys are present when nothing
    matches (0 and nulls). ``triggers`` says what starts it, ``can_disable``
    whether it may be turned off, and ``enabled`` whether it is on.
    """
    workflows = service.list_workflows()
    stats = _week_run_stats(utcnow())
    disabled = disabled_workflow_ids()
    for row in workflows:
        row.update(stats.get(row["id"], _NO_RUNS))
        row["triggers"] = triggers_for(row["id"])
        row["can_disable"] = can_disable(row["id"])
        row["enabled"] = row["id"] not in disabled
    return {"workflows": workflows, "count": len(workflows)}


def _lead_agent() -> Dict[str, Any]:
    """The lead a single-agent run is driven by, and the model it will use.

    The model is what ``get_playbook`` hands the run, so the reader shows what
    runs. The lead has no Agents-tab row, so there is no id, avatar or label to
    look up. The source is only claimed when it was checked: a model that came
    from ``chat_default`` or the provider default is plain "default".
    """
    resolved = target.resolve_component("investigation")
    if resolved is None:
        return {"role": LEAD_ROLE, "model": None, "model_source": None}
    try:
        assigned = INVESTIGATION_COMPONENT in get_registry().get_all_assignments()
    except Exception:  # noqa: BLE001
        assigned = False
    return {
        "role": LEAD_ROLE,
        "model": model_display_name(resolved[1]),
        "model_source": "assignment" if assigned else "default",
    }


def detail(service: WorkflowsService, workflow_id: str) -> Optional[Dict[str, Any]]:
    """One workflow in full, or ``None`` when there is no such workflow.

    Not-found is returned rather than raised: the status code is the router's
    business, and this module has two of them. A single-agent file workflow also
    names its lead; every other kind omits ``agent``.
    """
    workflow = service.get_workflow_dict(workflow_id, include_body=True)
    if (
        workflow
        and workflow["source"] == "file"
        and workflow["run_kind"] in SINGLE_AGENT_RUN_KINDS
    ):
        workflow["agent"] = _lead_agent()
    return workflow
