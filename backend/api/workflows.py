"""Workflows API endpoints for SOC workflow management and execution."""

from typing import Any, Dict, List, Optional

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Pydantic schemas
# -----------------------------------------------------------------------------


class WorkflowExecuteRequest(BaseModel):
    """Request to execute a workflow."""

    finding_id: Optional[str] = None
    case_id: Optional[str] = None
    context: Optional[str] = None
    hypothesis: Optional[str] = None


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
async def list_workflows():
    """
    List all available workflows (file-based + database-backed custom).

    Returns:
        { workflows: [...], count: int }
    """
    try:
        from services.workflows_service import get_workflows_service

        service = get_workflows_service()
        workflows = service.list_workflows()

        return {"workflows": workflows, "count": len(workflows)}
    except Exception as e:
        logger.error(f"Error listing workflows: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Static routes MUST come before parameterized {workflow_id} routes
@router.post("/workflows/reload")
async def reload_workflows():
    """
    Force reload all file-based workflows from disk.

    Does not affect database-backed custom workflows.
    """
    try:
        from services.workflows_service import get_workflows_service

        service = get_workflows_service()
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
async def list_custom_workflows(active_only: bool = True):
    """List database-backed custom workflows."""
    try:
        from services.custom_workflow_service import get_custom_workflow_service

        rows = get_custom_workflow_service().list(active_only=active_only)
        return {"workflows": rows, "count": len(rows)}
    except Exception as e:
        logger.error(f"Error listing custom workflows: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflows/custom", status_code=201)
async def create_custom_workflow(payload: CustomWorkflowCreate):
    """Create a new custom workflow."""
    try:
        from services.custom_workflow_service import get_custom_workflow_service

        service = get_custom_workflow_service()
        created = service.create(payload.model_dump())
        return created
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Error creating custom workflow")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflows/custom/{workflow_id}")
async def get_custom_workflow(workflow_id: str):
    """Fetch a single custom workflow."""
    try:
        from services.custom_workflow_service import get_custom_workflow_service

        wf = get_custom_workflow_service().get(workflow_id)
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
async def update_custom_workflow(workflow_id: str, payload: CustomWorkflowUpdate):
    """Update an existing custom workflow. Increments version."""
    try:
        from services.custom_workflow_service import get_custom_workflow_service

        service = get_custom_workflow_service()
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
async def delete_custom_workflow(workflow_id: str):
    """Soft-delete a custom workflow (sets is_active=False)."""
    try:
        from services.custom_workflow_service import get_custom_workflow_service

        ok = get_custom_workflow_service().delete(workflow_id)
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
async def generate_workflow(payload: WorkflowGenerateRequest):
    """
    Generate a draft custom workflow from a natural-language description.

    Does NOT save. Frontend can tweak the draft and POST to /workflows/custom.
    """
    try:
        from services.workflow_ai_generator import get_workflow_ai_generator

        result = await get_workflow_ai_generator().generate(payload.description)
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


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str):
    """
    Get full details for a specific workflow (custom or file-based).
    """
    try:
        from services.workflows_service import get_workflows_service

        service = get_workflows_service()
        workflow = service.get_workflow_dict(workflow_id, include_body=True)
        if not workflow:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow not found: {workflow_id}",
            )
        return workflow
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting workflow {workflow_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflows/{workflow_id}/execute")
async def execute_workflow(workflow_id: str, request: WorkflowExecuteRequest):
    """
    Execute a workflow (custom or file-based).

    Builds a composite prompt from the workflow definition and agent
    methodologies, then executes it via ClaudeService.run_agent_task().
    """
    try:
        from services.workflows_service import get_workflows_service

        service = get_workflows_service()

        workflow = service.get_workflow(workflow_id)
        if not workflow:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow not found: {workflow_id}",
            )

        parameters = {k: v for k, v in request.model_dump().items() if v is not None}

        if not parameters:
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
async def get_workflow_run(run_id: str):
    """Fetch a single workflow run by id.

    Includes the full ``result_summary`` plus the list of phase rows
    (``workflow_run_phases``) written by the phased execution loop
    (#128). For one-shot runs with no phase rows, ``phases`` is just
    an empty list.
    """
    from services.workflow_run_service import get_workflow_run_service

    run_service = get_workflow_run_service()
    row = run_service.get_run(run_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    row["phases"] = run_service.list_phases(run_id)
    return row


@router.post("/workflows/runs/{run_id}/resume")
async def resume_workflow_run(run_id: str, request: WorkflowRunResumeRequest):
    """Resume a paused workflow run (#128).

    Looks up the run's pending approval action, approves it, and
    re-enters the phase loop. If there is no pending approval action
    linked to the run, returns 409.
    """
    from services.approval_service import (
        ActionStatus,
        get_approval_service,
    )
    from services.workflow_run_service import get_workflow_run_service
    from services.workflows_service import get_workflows_service

    run_service = get_workflow_run_service()
    run = run_service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    if run.get("status") != "paused":
        raise HTTPException(
            status_code=409,
            detail=f"Run {run_id} is not paused (status={run.get('status')})",
        )

    approval_service = get_approval_service()
    pending = [
        a
        for a in approval_service.list_actions(
            status=ActionStatus.PENDING, workflow_run_id=run_id
        )
    ]
    if pending:
        approval_service.approve_action(
            pending[0].action_id,
            approved_by=request.approved_by or "analyst",
        )

    result = await get_workflows_service().resume_workflow(
        run_id,
        "approved",
        approved_by=request.approved_by or "analyst",
    )
    return result


@router.post("/workflows/runs/{run_id}/cancel")
async def cancel_workflow_run(run_id: str, request: WorkflowRunCancelRequest):
    """Cancel a paused or running workflow run (#128).

    Rejects any pending approval action on the run and finalises it
    as ``cancelled`` with the supplied reason.
    """
    from services.approval_service import (
        ActionStatus,
        get_approval_service,
    )
    from services.workflow_run_service import get_workflow_run_service
    from services.workflows_service import get_workflows_service

    run_service = get_workflow_run_service()
    run = run_service.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")

    approval_service = get_approval_service()
    pending = [
        a
        for a in approval_service.list_actions(
            status=ActionStatus.PENDING, workflow_run_id=run_id
        )
    ]
    for action in pending:
        approval_service.reject_action(
            action.action_id,
            reason=request.reason,
            rejected_by=request.rejected_by or "analyst",
        )

    if run.get("status") == "paused":
        result = await get_workflows_service().resume_workflow(
            run_id,
            "rejected",
            rejection_reason=request.reason,
            approved_by=request.rejected_by or "analyst",
        )
        return result

    # Running-but-not-paused runs: we can't interrupt the in-flight
    # Claude call here, but we can mark the row cancelled so history
    # reflects the user's intent. (Background-worker support would
    # let us actually stop execution; that's out of scope for #128.)
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
    }


@router.get("/workflows/{workflow_id}/runs")
async def list_workflow_runs(
    workflow_id: str,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """List past executions of ``workflow_id``, newest first.

    Omits ``result_summary`` from each entry so the listing stays
    light; use GET /workflows/runs/{run_id} for the full detail.
    """
    from services.workflow_run_service import get_workflow_run_service

    # Light-touch bounds so a buggy caller can't ask for 10k rows.
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    runs = get_workflow_run_service().list_runs(
        workflow_id=workflow_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"workflow_id": workflow_id, "runs": runs}
