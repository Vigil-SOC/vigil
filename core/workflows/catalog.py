"""The workflow catalog: what workflows exist, and what one of them is.

Two routers answer these reads and neither owns the answer. The frozen contract
serves them at ``/api/v1/workflows`` (``core/api/v1/workflows_router.py``); the
console serves the pre-version ``/api/workflows`` paths from its own router,
because ``/workflows/custom`` and ``/workflows/{workflow_id}`` are only
order-safe inside one router. So the reads live here, in the domain whose
language they speak, and both routers call down into them.

The detail read is the definition and nothing else, whatever the kind. Hunt
preflight (capabilities, pricing, budgets) is about executing a run, so it lives
in ``core.workflows.hunt_preflight`` behind its own console route.
"""

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import func

from core.storage.connection import get_db_manager
from core.storage.models import WorkflowRun
from core.time import utcnow
from core.workflows.workflows_service import WorkflowsService, is_hunt_like


def is_hunt(workflows: WorkflowsService, workflow_id: Optional[str]) -> bool:
    """True when a workflow drives the hunt hypothesis loop.

    A hunt writes no phase rows: it has beliefs to report, not steps.
    """
    if not workflow_id:
        return False
    definition = workflows.get_workflow(str(workflow_id))
    return definition is not None and is_hunt_like(definition.run_kind)


def _today_run_stats(now: datetime) -> Dict[str, Tuple[int, Optional[float]]]:
    """Runs started since UTC midnight, and the mean cost of those that finished.

    ``started_at`` is naive UTC, so the bound is too: an aware datetime
    TypeErrors against the column. A finished run that cost nothing is a real
    zero; null is only "nothing finished". Deleted rows stay out of both.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    db = get_db_manager()
    with db.session_scope() as session:
        rows = (
            session.query(
                WorkflowRun.workflow_id,
                func.count(WorkflowRun.run_id),
                func.avg(WorkflowRun.total_cost_usd).filter(
                    WorkflowRun.finished_at.isnot(None)
                ),
            )
            .filter(
                WorkflowRun.started_at >= midnight,
                WorkflowRun.deleted_at.is_(None),
            )
            .group_by(WorkflowRun.workflow_id)
            .all()
        )
    return {
        str(workflow_id): (int(count), None if mean is None else float(mean))
        for workflow_id, count, mean in rows
    }


def listing(service: WorkflowsService) -> Dict[str, Any]:
    """Every available workflow, file-based and database-backed alike.

    Each row carries today's run count and the mean cost of the finished
    ones. Both keys are present when nothing matches (0 and null).
    """
    workflows = service.list_workflows()
    stats = _today_run_stats(utcnow())
    for row in workflows:
        runs, mean = stats.get(row["id"], (0, None))
        row["runs_today"] = runs
        row["mean_cost_usd"] = mean
    return {"workflows": workflows, "count": len(workflows)}


def detail(service: WorkflowsService, workflow_id: str) -> Optional[Dict[str, Any]]:
    """One workflow in full, or ``None`` when there is no such workflow.

    Not-found is returned rather than raised: the status code is the router's
    business, and this module has two of them.
    """
    return service.get_workflow_dict(workflow_id, include_body=True)
