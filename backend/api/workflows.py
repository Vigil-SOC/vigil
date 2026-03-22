"""Workflows API endpoints for SOC workflow management and execution."""

from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import logging

router = APIRouter()
logger = logging.getLogger(__name__)


class WorkflowExecuteRequest(BaseModel):
    """Request to execute a workflow."""
    finding_id: Optional[str] = None
    case_id: Optional[str] = None
    context: Optional[str] = None
    hypothesis: Optional[str] = None


@router.get("/workflows")
async def list_workflows():
    """
    List all available workflows.

    Returns:
        List of workflows with metadata (name, description, agents, tools, use-case)
    """
    try:
        from services.workflows_service import get_workflows_service

        service = get_workflows_service()
        workflows = service.list_workflows()

        return {
            "workflows": workflows,
            "count": len(workflows),
        }
    except Exception as e:
        logger.error(f"Error listing workflows: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Static routes MUST come before parameterized {workflow_id} routes
@router.post("/workflows/reload")
async def reload_workflows():
    """
    Force reload all workflows from disk.

    Useful after adding or modifying WORKFLOW.md files without restarting the server.

    Returns:
        Updated workflow count
    """
    try:
        from services.workflows_service import get_workflows_service

        service = get_workflows_service()
        service.reload()
        workflows = service.list_workflows()

        return {
            "success": True,
            "message": f"Reloaded {len(workflows)} workflows",
            "count": len(workflows),
        }
    except Exception as e:
        logger.error(f"Error reloading workflows: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflows/{workflow_id}")
async def get_workflow(workflow_id: str):
    """
    Get full details for a specific workflow, including the markdown body.

    Args:
        workflow_id: The workflow ID (directory name, e.g., 'incident-response')

    Returns:
        Full workflow details with metadata and body
    """
    try:
        from services.workflows_service import get_workflows_service

        service = get_workflows_service()
        workflow = service.get_workflow_dict(workflow_id, include_body=True)

        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow not found: {workflow_id}")

        return workflow
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting workflow {workflow_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/workflows/{workflow_id}/execute")
async def execute_workflow(workflow_id: str, request: WorkflowExecuteRequest):
    """
    Execute a workflow.

    Builds a composite prompt from the workflow definition and agent methodologies,
    then executes it via ClaudeService.run_agent_task().

    Args:
        workflow_id: The workflow ID to execute
        request: Execution parameters (finding_id, case_id, context, hypothesis)

    Returns:
        Execution result with workflow output, tool calls, and metadata
    """
    try:
        from services.workflows_service import get_workflows_service

        service = get_workflows_service()

        # Verify workflow exists
        workflow = service.get_workflow(workflow_id)
        if not workflow:
            raise HTTPException(status_code=404, detail=f"Workflow not found: {workflow_id}")

        # Build parameters dict
        parameters = {
            k: v for k, v in request.model_dump().items() if v is not None
        }

        if not parameters:
            raise HTTPException(
                status_code=400,
                detail="At least one parameter required: finding_id, case_id, context, or hypothesis"
            )

        # Execute the workflow
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
