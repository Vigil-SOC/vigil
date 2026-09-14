"""Workflows — versioned contract surface (``/api/v1/workflows``).

The catalog: list the available playbooks and read one. That is the whole
external contract for workflows — "what workflows exist and what does this one
do". Everything else on the unversioned router in
``core/workflows/workflows_router.py`` is console/authoring/control:

* custom-workflow CRUD and AI generation (authoring UI),
* ``reload`` (a dev button),
* ``execute`` / ``resume`` / ``cancel`` / ``narrate`` / ``delete`` and the
  workflow-run reads — these operate on *workflow runs*, which are the console
  history view of an execution. The frozen **run** contract is the agent run
  (``/api/agent-runs``), the engine record; a hunt is both an agent run and a
  workflow run joined by ``run_id``, so freezing the workflow-run surface too
  would promise two overlapping shapes for one hunt.

``_is_hunt`` lives here because the contract ``get_workflow`` needs it; the
console router imports it back (services stays; core -> core is fine).
"""

import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException

from core.deps import provide_mcp_registry, provide_workflows
from core.routing import Auth, RouterMeta
from core.workflows.workflows_service import WorkflowsService

router = APIRouter()

ROUTER_META = RouterMeta(
    # Additive, NOT legacy-mounted. The catalog read is GET /{workflow_id}, and
    # the console router keeps sibling literals under /api/workflows (notably
    # GET /workflows/custom, which looks like a {workflow_id}). A parameterised
    # route and a literal are only order-safe inside ONE router; mounting this
    # router's /{workflow_id} at /api/workflows as well would put them in two
    # routers and make first-match depend on mount order — exactly what
    # test_no_cross_router_path_shadowing forbids. So the frozen catalog lives
    # only at /api/v1/workflows here (the source of truth), and the console's
    # own GET /workflows and GET /workflows/{id} delegate to these functions,
    # staying in-router with /workflows/custom.
    prefix="/api/v1/workflows",
    tags=["workflows"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)


def _is_hunt(workflows: WorkflowsService, workflow_id: Optional[str]) -> bool:
    """True when a workflow drives the hunt hypothesis loop.

    A hunt writes no phase rows: it has beliefs to report, not steps. Shared
    with the console router's run read, which imports it from here.
    """
    from core.workflows.workflows_service import is_hunt_like

    if not workflow_id:
        return False
    definition = workflows.get_workflow(str(workflow_id))
    return definition is not None and is_hunt_like(definition.run_kind)


# Read from the resolver, not restated, so it cannot drift from what runs are
# built on.
def _hunt_defaults() -> Tuple[int, float]:
    from core.workflows.playbook_resolver import HUNT_BUDGETS, HUNT_THRESHOLDS

    return HUNT_THRESHOLDS["max_iterations"], HUNT_BUDGETS["max_cost_usd"]


# Best effort: a registry that cannot be read reports nothing missing rather
# than blocking the modal.
def _capabilities(registry: Any) -> Dict[str, Any]:
    from core.workflows.playbook_resolver import capability_report

    try:
        return capability_report(registry)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read bound capabilities: %s", exc)
        return {"bound": [], "unbound": []}


# What the run will be charged at, and how confidently. An unpriced model is
# refused a few calls in, correctly but after the spend, so it is said here.
def _pricing() -> Dict[str, Any]:
    from core.llm.cost.pricing_router import priced_as
    from core.llm.defaults import DEFAULT_MODEL
    from core.llm.providers.registry import get_registry

    try:
        provider, model = priced_as("bifrost", DEFAULT_MODEL)
        source = get_registry().get_pricing_source(model, provider)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read the rate for the default model: %s", exc)
        return {"model": DEFAULT_MODEL, "source": "unknown"}
    return {"model": DEFAULT_MODEL, "source": source}


@router.get("/")
async def list_workflows(service: WorkflowsService = Depends(provide_workflows)):
    """
    List all available workflows (file-based + database-backed custom).

    Returns:
        { workflows: [...], count: int }
    """
    workflows = service.list_workflows()
    return {"workflows": workflows, "count": len(workflows)}


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    service: WorkflowsService = Depends(provide_workflows),
    registry=Depends(provide_mcp_registry),
):
    """
    Get full details for a specific workflow (custom or file-based).
    """
    workflow = service.get_workflow_dict(workflow_id, include_body=True)
    if not workflow:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow not found: {workflow_id}",
        )
    # Only a hunt has turns to budget or capabilities to be missing. Answered
    # here so the console says both before the operator spends anything.
    if _is_hunt(service, workflow_id):
        workflow["capabilities"] = _capabilities(registry)
        workflow["pricing"] = _pricing()
        workflow["budgets"] = {
            "max_iterations": _hunt_defaults()[0],
            "max_cost_usd": _hunt_defaults()[1],
        }
    return workflow
