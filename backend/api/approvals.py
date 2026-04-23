"""Approvals API — list/approve/reject pending human-in-the-loop actions (#128).

Workflow phase approvals surface here alongside any other pending
action the ``ApprovalService`` is tracking (e.g. daemon-triggered
containment actions). Approving a workflow-linked action auto-resumes
the paused run; rejecting cancels it with the supplied reason.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ApproveRequest(BaseModel):
    approved_by: Optional[str] = Field(
        default=None,
        description="Identity of the approving analyst. Defaults to 'analyst'.",
    )


class RejectRequest(BaseModel):
    reason: str = Field(..., description="Why the action is being rejected.")
    rejected_by: Optional[str] = Field(
        default=None,
        description="Identity of the rejecting analyst. Defaults to 'analyst'.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pending_to_dict(action: Any) -> Dict[str, Any]:
    """Normalise a ``PendingAction`` dataclass to a response dict."""
    return {
        "action_id": action.action_id,
        "action_type": action.action_type,
        "title": action.title,
        "description": action.description,
        "target": action.target,
        "confidence": action.confidence,
        "reason": action.reason,
        "evidence": action.evidence,
        "created_at": action.created_at,
        "created_by": action.created_by,
        "requires_approval": action.requires_approval,
        "status": action.status,
        "approved_at": action.approved_at,
        "approved_by": action.approved_by,
        "executed_at": action.executed_at,
        "execution_result": action.execution_result,
        "rejection_reason": action.rejection_reason,
        "parameters": action.parameters,
        "workflow_run_id": action.workflow_run_id,
        "workflow_phase_id": action.workflow_phase_id,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/approvals")
async def list_approvals(
    status: Optional[str] = Query(
        default=None,
        description=(
            "Filter by status: pending | approved | rejected | executed | failed."
        ),
    ),
    workflow_run_id: Optional[str] = Query(
        default=None,
        description="Restrict to approvals linked to this workflow run.",
    ),
    limit: int = Query(default=100, ge=1, le=500),
):
    """List approval actions, newest first."""
    from services.approval_service import (
        ActionStatus,
        get_approval_service,
    )

    status_enum: Optional[ActionStatus] = None
    if status:
        try:
            status_enum = ActionStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    service = get_approval_service()
    actions = service.list_actions(
        status=status_enum,
        workflow_run_id=workflow_run_id,
        limit=limit,
    )
    return {
        "count": len(actions),
        "actions": [_pending_to_dict(a) for a in actions],
    }


@router.get("/approvals/pending")
async def list_pending_approvals() -> Dict[str, List[Dict[str, Any]]]:
    """Shortcut: only actions with ``status=pending`` and
    ``requires_approval=True``. Used by the AI Decisions approvals tab."""
    from services.approval_service import get_approval_service

    service = get_approval_service()
    actions = service.list_pending_approvals()
    return {"actions": [_pending_to_dict(a) for a in actions]}


@router.get("/approvals/{action_id}")
async def get_approval(action_id: str):
    """Fetch a single approval action."""
    from services.approval_service import get_approval_service

    action = get_approval_service().get_action(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail=f"Approval not found: {action_id}")
    return _pending_to_dict(action)


@router.post("/approvals/{action_id}/approve")
async def approve_action(action_id: str, request: ApproveRequest):
    """Approve a pending action.

    If the action is linked to a paused workflow run, the run resumes
    automatically and the resume result is included in the response.
    """
    from services.approval_service import get_approval_service
    from services.workflows_service import get_workflows_service

    service = get_approval_service()
    action = service.get_action(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail=f"Approval not found: {action_id}")

    approved_by = request.approved_by or "analyst"
    updated = service.approve_action(action_id, approved_by=approved_by)
    if updated is None:
        raise HTTPException(status_code=500, detail="Failed to approve action")

    response: Dict[str, Any] = {
        "action": _pending_to_dict(updated),
        "resume_result": None,
    }

    if updated.workflow_run_id:
        resume = await get_workflows_service().resume_workflow(
            updated.workflow_run_id,
            "approved",
            approved_by=approved_by,
        )
        response["resume_result"] = resume

    return response


@router.post("/approvals/{action_id}/reject")
async def reject_action(action_id: str, request: RejectRequest):
    """Reject a pending action.

    If the action is linked to a paused workflow run, the run is
    cancelled with the supplied reason.
    """
    from services.approval_service import get_approval_service
    from services.workflows_service import get_workflows_service

    service = get_approval_service()
    action = service.get_action(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail=f"Approval not found: {action_id}")

    rejected_by = request.rejected_by or "analyst"
    updated = service.reject_action(
        action_id, reason=request.reason, rejected_by=rejected_by
    )
    if updated is None:
        raise HTTPException(status_code=500, detail="Failed to reject action")

    response: Dict[str, Any] = {
        "action": _pending_to_dict(updated),
        "resume_result": None,
    }

    if updated.workflow_run_id:
        resume = await get_workflows_service().resume_workflow(
            updated.workflow_run_id,
            "rejected",
            rejection_reason=request.reason,
            approved_by=rejected_by,
        )
        response["resume_result"] = resume

    return response
