"""Workflows API endpoints for SOC workflow management and execution."""

import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.agents.projections import read_projection
from core.deps import (
    provide_approvals,
    provide_custom_workflows,
    provide_mcp_registry,
    provide_workflow_ai,
    provide_workflow_runs,
    provide_workflows,
)
from core.response.approval_service import ApprovalService
from core.routing import Auth, RouterMeta
from core.workflows.custom_workflow_service import CustomWorkflowService
from core.workflows.workflow_ai_generator import WorkflowAIGenerator
from core.workflows.workflow_run_service import WorkflowRunService
from core.workflows.workflows_service import WorkflowsService

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api",
    tags=["workflows"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)

# What gives a run something to work on. A turn count or a cost ceiling says how far
# to go and never where, so neither is a target.
TARGET_PARAMS = frozenset({"finding_id", "case_id", "context", "hypothesis"})


# -----------------------------------------------------------------------------
# Pydantic schemas
# -----------------------------------------------------------------------------


class WorkflowExecuteRequest(BaseModel):
    """Request to execute a workflow."""

    finding_id: Optional[str] = None
    case_id: Optional[str] = None
    context: Optional[str] = None
    hypothesis: Optional[str] = None
    # Turns, not model calls. Bounded so a typo cannot enqueue an hour of spend.
    iterations: Optional[int] = Field(default=None, ge=1, le=40)
    # What the caller will spend on this question, which is not a property of the
    # definition. Bounded because a mistyped ceiling is money.
    max_cost_usd: Optional[float] = Field(default=None, gt=0, le=100)
    # Whether the hunt stops and asks before it spends. The policy defaults to auto,
    # so a headless run advances with nobody at a terminal.
    approve_hypotheses: Optional[bool] = None


class WorkflowPhaseSchema(BaseModel):
    phase_id: Optional[str] = None
    order: Optional[int] = None
    agent_id: str
    name: str
    purpose: Optional[str] = ""
    tools: List[str] = Field(default_factory=list)
    steps: List[str] = Field(default_factory=list)
    expected_output: Optional[str] = ""
    timeout_seconds: Optional[int] = 300
    approval_required: bool = False
    conditions: Optional[Any] = None  # reserved for branching
    parallel_group: Optional[str] = None  # reserved for parallel paths


class CustomWorkflowCreate(BaseModel):
    name: str
    description: str
    use_case: Optional[str] = ""
    trigger_examples: List[str] = Field(default_factory=list)
    phases: List[WorkflowPhaseSchema] = Field(default_factory=list)
    graph_layout: Dict[str, Any] = Field(default_factory=dict)
    created_by: Optional[str] = None


class CustomWorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    use_case: Optional[str] = None
    trigger_examples: Optional[List[str]] = None
    phases: Optional[List[WorkflowPhaseSchema]] = None
    graph_layout: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class WorkflowGenerateRequest(BaseModel):
    description: str


class WorkflowRunResumeRequest(BaseModel):
    """Optional payload when manually resuming a paused run."""

    approved_by: Optional[str] = None


class WorkflowRunCancelRequest(BaseModel):
    """Payload when cancelling a paused / running run from the UI."""

    reason: str
    rejected_by: Optional[str] = None


# -----------------------------------------------------------------------------
# Read-only discovery endpoints (existing + extended)
# -----------------------------------------------------------------------------


@router.get("/workflows")
async def list_workflows(service: WorkflowsService = Depends(provide_workflows)):
    """
    List all available workflows (file-based + database-backed custom).

    Returns:
        { workflows: [...], count: int }
    """
    try:
        workflows = service.list_workflows()

        return {"workflows": workflows, "count": len(workflows)}
    except Exception as e:
        logger.error(f"Error listing workflows: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Static routes MUST come before parameterized {workflow_id} routes
@router.post("/workflows/reload")
async def reload_workflows(service: WorkflowsService = Depends(provide_workflows)):
    """
    Force reload all file-based workflows from disk.

    Does not affect database-backed custom workflows.
    """
    try:
        service.reload()
        workflows = service.list_workflows()

        return {
            "success": True,
            "message": f"Reloaded workflows (total={len(workflows)})",
            "count": len(workflows),
        }
    except Exception as e:
        logger.error(f"Error reloading workflows: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Custom workflow CRUD (database-backed)
# -----------------------------------------------------------------------------


@router.get("/workflows/custom")
async def list_custom_workflows(
    active_only: bool = True,
    service: CustomWorkflowService = Depends(provide_custom_workflows),
):
    """List database-backed custom workflows."""
    try:
        rows = service.list(active_only=active_only)
        return {"workflows": rows, "count": len(rows)}
    except Exception as e:
        logger.error(f"Error listing custom workflows: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflows/custom", status_code=201)
async def create_custom_workflow(
    payload: CustomWorkflowCreate,
    service: CustomWorkflowService = Depends(provide_custom_workflows),
):
    """Create a new custom workflow."""
    try:
        created = service.create(payload.model_dump())
        return created
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error creating custom workflow")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflows/custom/{workflow_id}")
async def get_custom_workflow(
    workflow_id: str,
    service: CustomWorkflowService = Depends(provide_custom_workflows),
):
    """Fetch a single custom workflow."""
    try:
        wf = service.get(workflow_id)
        if not wf:
            raise HTTPException(
                status_code=404,
                detail=f"Custom workflow not found: {workflow_id}",
            )
        return wf
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching custom workflow")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/workflows/custom/{workflow_id}")
async def update_custom_workflow(
    workflow_id: str,
    payload: CustomWorkflowUpdate,
    service: CustomWorkflowService = Depends(provide_custom_workflows),
):
    """Update an existing custom workflow. Increments version."""
    try:
        updates = {k: v for k, v in payload.model_dump().items() if v is not None}
        updated = service.update(workflow_id, updates)
        if not updated:
            raise HTTPException(
                status_code=404,
                detail=f"Custom workflow not found: {workflow_id}",
            )
        return updated
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error updating custom workflow")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/workflows/custom/{workflow_id}")
async def delete_custom_workflow(
    workflow_id: str,
    service: CustomWorkflowService = Depends(provide_custom_workflows),
):
    """Soft-delete a custom workflow (sets is_active=False)."""
    try:
        ok = service.delete(workflow_id)
        if not ok:
            raise HTTPException(
                status_code=404,
                detail=f"Custom workflow not found: {workflow_id}",
            )
        return {"success": True, "workflow_id": workflow_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error deleting custom workflow")
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# AI-assisted generation
# -----------------------------------------------------------------------------


@router.post("/workflows/generate")
async def generate_workflow(
    payload: WorkflowGenerateRequest,
    generator: WorkflowAIGenerator = Depends(provide_workflow_ai),
):
    """
    Generate a draft custom workflow from a natural-language description.

    Does NOT save. Frontend can tweak the draft and POST to /workflows/custom.
    """
    try:
        result = await generator.generate(payload.description)
        if not result.get("success"):
            raise HTTPException(
                status_code=502,
                detail=result.get("error") or "Workflow generation failed",
            )
        return {"draft": result["draft"]}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error generating workflow")
        raise HTTPException(status_code=500, detail=str(e))


# -----------------------------------------------------------------------------
# Parameterized discovery/execution routes (keep at bottom so specific paths
# like /workflows/custom and /workflows/reload match first)
# -----------------------------------------------------------------------------


# Read from the resolver, not restated, so it cannot drift from what runs are built on.
def _hunt_defaults() -> Tuple[int, float]:
    from core.workflows.playbook_resolver import HUNT_BUDGETS, HUNT_THRESHOLDS

    return HUNT_THRESHOLDS["max_iterations"], HUNT_BUDGETS["max_cost_usd"]


# Best effort: a registry that cannot be read reports nothing missing rather than
# blocking the modal.
def _capabilities(registry: Any) -> Dict[str, Any]:
    from core.workflows.playbook_resolver import capability_report

    try:
        return capability_report(registry)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read bound capabilities: %s", exc)
        return {"bound": [], "unbound": []}


# What the run will be charged at, and how confidently. An unpriced model is refused a
# few calls in, correctly but after the spend, so it is said here instead.
def _pricing() -> Dict[str, Any]:
    from core.llm.cost.pricing_router import _priced_as
    from core.llm.defaults import DEFAULT_MODEL
    from core.llm.providers.registry import get_registry

    try:
        provider, model = _priced_as("bifrost", DEFAULT_MODEL)
        source = get_registry().get_pricing_source(model, provider)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not read the rate for the default model: %s", exc)
        return {"model": DEFAULT_MODEL, "source": "unknown"}
    return {"model": DEFAULT_MODEL, "source": source}


@router.get("/workflows/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    service: WorkflowsService = Depends(provide_workflows),
    registry=Depends(provide_mcp_registry),
):
    """
    Get full details for a specific workflow (custom or file-based).
    """
    try:
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting workflow {workflow_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflows/{workflow_id}/execute")
async def execute_workflow(
    workflow_id: str,
    request: WorkflowExecuteRequest,
    service: WorkflowsService = Depends(provide_workflows),
):
    """
    Execute a workflow (custom or file-based).

    Builds a composite prompt from the workflow definition and agent
    methodologies, then executes it via ClaudeService.run_agent_task().
    """
    try:
        workflow = service.get_workflow(workflow_id)
        if not workflow:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow not found: {workflow_id}",
            )

        parameters = {k: v for k, v in request.model_dump().items() if v is not None}

        if not TARGET_PARAMS & parameters.keys():
            raise HTTPException(
                status_code=400,
                detail=(
                    "At least one parameter required: finding_id, case_id, "
                    "context, or hypothesis"
                ),
            )

        # Pass the caller as triggered_by so the workflow_runs row has a
        # useful audit marker. "api" is a safe default when auth isn't
        # surfacing a concrete user identity here (DEV_MODE / system
        # triggers). Daemon invocations can override by calling the
        # service layer directly.
        result = await service.execute_workflow(
            workflow_id, parameters, triggered_by="api"
        )

        if not result.get("success"):
            error = result.get("error", "Unknown error during workflow execution")
            raise HTTPException(status_code=500, detail=error)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing workflow {workflow_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Run history (#127)
# ---------------------------------------------------------------------------


@router.get("/workflows/runs/{run_id}")
async def get_workflow_run(
    run_id: str,
    run_service: WorkflowRunService = Depends(provide_workflow_runs),
    workflows: WorkflowsService = Depends(provide_workflows),
):
    """Fetch a single workflow run by id.

    Includes the full ``result_summary`` plus the list of phase rows
    (``workflow_run_phases``) written by the phased execution loop
    (#128). For one-shot runs with no phase rows, ``phases`` is just
    an empty list.
    """
    row = run_service.get_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    row["phases"] = run_service.list_phases(run_id)
    if _is_hunt(workflows, row.get("workflow_id")):
        row["hunt"] = await read_projection(run_id)
    return row


# A hunt writes no phase rows: it has beliefs to report, not steps. The agent
# layer owns them, so they are read from it rather than folded here.
def _is_hunt(workflows: WorkflowsService, workflow_id: Optional[str]) -> bool:
    from core.workflows.workflows_service import HUNT_RUN_KIND

    if not workflow_id:
        return False
    definition = workflows.get_workflow(str(workflow_id))
    return definition is not None and definition.run_kind == HUNT_RUN_KIND


@router.post("/workflows/runs/{run_id}/resume")
async def resume_workflow_run(
    run_id: str,
    request: WorkflowRunResumeRequest,
    run_service: WorkflowRunService = Depends(provide_workflow_runs),
    approval_service: ApprovalService = Depends(provide_approvals),
    workflows: WorkflowsService = Depends(provide_workflows),
):
    """Resume a paused workflow run (#128).

    Looks up the run's pending approval action, approves it, and
    re-enters the phase loop. If there is no pending approval action
    linked to the run, returns 409.
    """
    from core.response.approval_service import ActionStatus
    from core.workflows.run_resume import resume_run

    run = run_service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    if run.get("status") != "paused":
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} is not paused (status={run.get('status')})",
        )

    approved_by = request.approved_by or "analyst"
    pending = approval_service.list_actions(
        status=ActionStatus.PENDING, workflow_run_id=run_id
    )
    if not pending:
        raise HTTPException(
            status_code=409, detail=f"Run {run_id} has no pending approval"
        )

    approval_service.approve_action(pending[0].action_id, approved_by=approved_by)
    return await resume_run(run_id, pending[0].action_id, approved_by)


@router.post("/workflows/runs/{run_id}/cancel")
async def cancel_workflow_run(
    run_id: str,
    request: WorkflowRunCancelRequest,
    run_service: WorkflowRunService = Depends(provide_workflow_runs),
    approval_service: ApprovalService = Depends(provide_approvals),
    workflows: WorkflowsService = Depends(provide_workflows),
):
    """Cancel a paused or running workflow run (#128).

    Rejects any pending approval action on the run and finalises it
    as ``cancelled`` with the supplied reason.
    """
    from core.response.approval_service import ActionStatus
    from core.workflows.run_cancel import stop_run
    from core.workflows.run_resume import resume_run

    run = run_service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    rejected_by = request.rejected_by or "analyst"
    pending = approval_service.list_actions(
        status=ActionStatus.PENDING, workflow_run_id=run_id
    )
    for action in pending:
        approval_service.reject_action(
            action.action_id, reason=request.reason, rejected_by=rejected_by
        )

    # A rejection ends the run, but the agent layer is what ends it: this hands
    # the decision over and that side journals it and stops.
    if run.get("status") == "paused" and pending:
        return await resume_run(run_id, pending[0].action_id, rejected_by)

    # Ask the run to stop, then make sure it does: the abort lets a hunt settle itself
    # and write a report, and the escalation behind it covers a worker that cannot.
    stopped = stop_run(run_id, request.reason, rejected_by)

    run_service.finalize_run(
        run_id,
        status="cancelled",
        error=f"Cancelled: {request.reason}",
    )
    return {
        "success": True,
        "status": "cancelled",
        "run_id": run_id,
        "rejection_reason": request.reason,
        **stopped,
    }


@router.post("/workflows/runs/{run_id}/narrate")
async def narrate_workflow_run(
    run_id: str,
    run_service: WorkflowRunService = Depends(provide_workflow_runs),
):
    """Write a fresh account of ``run_id`` from its ledger.

    Answerable whichever state the run is in, a finished one included:
    the write-up reads the record and appends to it, so it needs neither
    the run's lease nor its loop.
    """
    from core.agents.projections import write_narrative

    if not run_service.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    try:
        return {"success": True, "narrative": await write_narrative(run_id)}
    except Exception as exc:  # noqa: BLE001 — the operator is owed the reason
        logger.error("could not write up run %s: %s", run_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.delete("/workflows/runs/{run_id}")
async def delete_workflow_run(
    run_id: str,
    run_service: WorkflowRunService = Depends(provide_workflow_runs),
):
    """Remove a finished run from the listings.

    A mark, not a drop: the row and the agent ledger behind it stay
    readable by run_id, because that ledger is the only account of what
    the agents did. A run still in flight is refused — cancel it first,
    so nothing is hidden while a worker is still writing to it.
    """
    run = run_service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    if run.get("status") in ("running", "paused"):
        raise HTTPException(
            status_code=409,
            detail="This run has not finished. Cancel it before removing it.",
        )
    if not run_service.delete_run(run_id):
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return {"success": True, "run_id": run_id}


@router.get("/workflows/{workflow_id}/runs")
async def list_workflow_runs(
    workflow_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    run_service: WorkflowRunService = Depends(provide_workflow_runs),
):
    """List past executions of ``workflow_id``, newest first.

    Omits ``result_summary`` from each entry so the listing stays
    light; use GET /workflows/runs/{run_id} for the full detail.
    """
    # Light-touch bounds so a buggy caller can't ask for 10k rows.
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    runs = run_service.list_runs(
        workflow_id=workflow_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"workflow_id": workflow_id, "runs": runs}
