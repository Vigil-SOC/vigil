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

from typing import Any, Dict, Optional

from core.workflows.workflows_service import WorkflowsService, is_hunt_like


def is_hunt(workflows: WorkflowsService, workflow_id: Optional[str]) -> bool:
    """True when a workflow drives the hunt hypothesis loop.

    A hunt writes no phase rows: it has beliefs to report, not steps.
    """
    if not workflow_id:
        return False
    definition = workflows.get_workflow(str(workflow_id))
    return definition is not None and is_hunt_like(definition.run_kind)


def listing(service: WorkflowsService) -> Dict[str, Any]:
    """Every available workflow, file-based and database-backed alike."""
    workflows = service.list_workflows()
    return {"workflows": workflows, "count": len(workflows)}


def detail(service: WorkflowsService, workflow_id: str) -> Optional[Dict[str, Any]]:
    """One workflow in full, or ``None`` when there is no such workflow.

    Not-found is returned rather than raised: the status code is the router's
    business, and this module has two of them.
    """
    return service.get_workflow_dict(workflow_id, include_body=True)
