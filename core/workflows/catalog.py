"""The workflow catalog: what workflows exist, and what one of them is.

Two routers answer these reads and neither owns the answer. The frozen contract
serves them at ``/api/v1/workflows`` (``core/api/v1/workflows_router.py``); the
console serves the pre-version ``/api/workflows`` paths from its own router,
because ``/workflows/custom`` and ``/workflows/{workflow_id}`` are only
order-safe inside one router. So the reads live here, in the domain whose
language they speak, and both routers call down into them.

Hunt preflight (capabilities, pricing, budgets) rides on the detail read: the
console asks before a run starts, so an operator learns a hunt will run without
a SIEM, and at what rate, while it still costs nothing.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from core.workflows.workflows_service import WorkflowsService

logger = logging.getLogger(__name__)


def is_hunt(workflows: WorkflowsService, workflow_id: Optional[str]) -> bool:
    """True when a workflow drives the hunt hypothesis loop.

    A hunt writes no phase rows: it has beliefs to report, not steps.
    """
    from core.workflows.workflows_service import is_hunt_like

    if not workflow_id:
        return False
    definition = workflows.get_workflow(str(workflow_id))
    return definition is not None and is_hunt_like(definition.run_kind)


# Read from the resolver, not restated, so it cannot drift from what runs are
# built on.
def hunt_defaults() -> Tuple[int, float]:
    from core.workflows.playbook_resolver import HUNT_BUDGETS, HUNT_THRESHOLDS

    return HUNT_THRESHOLDS["max_iterations"], HUNT_BUDGETS["max_cost_usd"]


# Best effort: a registry that cannot be read reports nothing missing rather
# than blocking the modal.
def capabilities(registry: Any) -> Dict[str, Any]:
    from core.workflows.playbook_resolver import capability_report

    try:
        return capability_report(registry)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read bound capabilities: %s", exc)
        return {"bound": [], "unbound": []}


# What the run will be charged at, and how confidently. An unpriced model is
# refused a few calls in, correctly but after the spend, so it is said here.
def pricing() -> Dict[str, Any]:
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


def listing(service: WorkflowsService) -> Dict[str, Any]:
    """Every available workflow, file-based and database-backed alike."""
    workflows = service.list_workflows()
    return {"workflows": workflows, "count": len(workflows)}


def detail(
    service: WorkflowsService,
    registry: Any,
    workflow_id: str,
) -> Optional[Dict[str, Any]]:
    """One workflow in full, or ``None`` when there is no such workflow.

    Not-found is returned rather than raised: the status code is the router's
    business, and this module has two of them.
    """
    workflow = service.get_workflow_dict(workflow_id, include_body=True)
    if not workflow:
        return None
    # NOTE (contract smell, tracked): capabilities/pricing/budgets are agent-run
    # preflight, not catalog data — they describe *executing* a hunt, not the
    # workflow definition. They ride here to feed the console's start-a-hunt
    # modal. Safe to move later without breaking the freeze: the detail endpoint
    # has no response_model, so the contract snapshot pins the operation, not
    # these fields. Follow-up: move hunt preflight off the catalog read.
    # Only a hunt has turns to budget or capabilities to be missing.
    if is_hunt(service, workflow_id):
        max_iterations, max_cost_usd = hunt_defaults()
        workflow["capabilities"] = capabilities(registry)
        workflow["pricing"] = pricing()
        workflow["budgets"] = {
            "max_iterations": max_iterations,
            "max_cost_usd": max_cost_usd,
        }
    return workflow
