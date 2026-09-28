"""Hunt preflight: what a hunt will cost at most, and what it cannot look at.

The console asks before a run starts, so an operator learns a hunt will run
without a SIEM, and at what rate, while it still costs nothing. This describes
*executing* a hunt, not the workflow definition, so it is console wiring served
at ``GET /api/workflows/{workflow_id}/preflight`` and kept off the frozen
catalog read.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from core.workflows.workflows_service import WorkflowsService, is_hunt_like

logger = logging.getLogger(__name__)


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


def preflight(
    service: WorkflowsService,
    registry: Any,
    workflow_id: str,
) -> Optional[Dict[str, Any]]:
    """``{capabilities, pricing, budgets}`` for a hunt, ``{}`` for any other
    kind, ``None`` when there is no such workflow.

    Only a hunt has turns to budget or capabilities to be missing, so a
    non-hunt answers empty rather than 404: the workflow exists, there is just
    nothing to warn about before it runs.
    """
    definition = service.get_workflow(workflow_id)
    if definition is None:
        return None
    if not is_hunt_like(definition.run_kind):
        return {}
    max_iterations, max_cost_usd = hunt_defaults()
    return {
        "capabilities": capabilities(registry),
        "pricing": pricing(),
        "budgets": {"max_iterations": max_iterations, "max_cost_usd": max_cost_usd},
    }
