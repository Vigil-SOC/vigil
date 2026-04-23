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

        result = await service.execute_workflow(workflow_id, parameters)

        if not result.get("success"):
            error = result.get("error", "Unknown error during workflow execution")
            raise HTTPException(status_code=500, detail=error)

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing workflow {workflow_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
