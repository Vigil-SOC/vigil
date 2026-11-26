"""Approvals API — list/approve/reject pending human-in-the-loop actions (#128).

Workflow phase approvals surface here alongside any other pending
action the ``ApprovalService`` is tracking. Approving a workflow-linked
action auto-resumes the paused run; rejecting cancels it with the supplied
reason. Approving an isolation or account disable runs it on the vendor's
own ID at once (#5059); Release or Enable on an executed one undoes it (#5060).
"""

import logging
from dataclasses import asdict
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate
from core.auth.tool_principal import Claims
from core.deps import provide_approvals, provide_mcp_registry
from core.integrations.mcp.registry import MCPRegistry
from core.response import containment
from core.response.approval_decisions import (
    ActionNotFound,
    decide_reject,
    record_decision,
    rollout_decided,
)
from core.response.approval_service import (
    CONTAINMENT_TYPES,
    AlreadyDecided,
    ApprovalService,
    FourEyesRefused,
    NotUndoable,
    needs_you,
    on_case,
    screen_action,
    screen_actions,
)
from core.routing import Auth, RouterMeta
from core.storage.models import User

router = APIRouter()

_DECIDE = [permission_gate("ai_decisions.approve")]
router.dependencies.append(permission_gate("ai_decisions.read"))

ROUTER_META = RouterMeta(
    # Versioned contract surface. The whole approvals router is external — every
    # route is a durable record or an act on it (list, get, approve, reject) —
    # so it moves wholesale rather than splitting. Routes carry the full
    # ``/approvals`` path, so the prefix is the version root, not ``/api/v1/approvals``.
    prefix="/api/v1",
    tags=["approvals"],
    auth=Auth.REQUIRED,
    legacy_prefixes=("/api",),
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ApproveRequest(BaseModel):
    approved_by: Optional[str] = Field(
        default=None,
        description="Identity of the approving analyst. Defaults to 'analyst'.",
    )
    candidate: Optional[str] = Field(
        default=None,
        description=(
            "An isolation or account disable: the id of the stored candidate "
            "(parameters.containment.candidates) to act on. May be omitted when "
            "there is one."
        ),
    )


class RejectRequest(BaseModel):
    reason: str = Field(..., description="Why the action is being rejected.")
    rejected_by: Optional[str] = Field(
        default=None,
        description="Identity of the rejecting analyst. Defaults to 'analyst'.",
    )


class UndoRequest(BaseModel):
    reason: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Why the containment is being undone; the next run reads it.",
    )


class PendingActionResponse(BaseModel):
    """Frozen shape of an approval action.

    Value types are permissive where the underlying dataclass carries open JSON
    (evidence, parameters, execution_result); the key set is the promise.
    """

    action_id: Optional[str] = None
    action_type: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    target: Optional[Any] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None
    evidence: Optional[Any] = None
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    requires_approval: Optional[bool] = None
    status: Optional[str] = None
    approved_at: Optional[str] = None
    approved_by: Optional[str] = None
    executed_at: Optional[str] = None
    execution_result: Optional[Any] = None
    rejection_reason: Optional[str] = None
    parameters: Optional[Any] = None
    workflow_run_id: Optional[str] = None
    workflow_phase_id: Optional[str] = None
    reversibility: Optional[str] = None
    idempotency_key: Optional[str] = None
    restricted: bool = Field(
        default=False,
        description="An action on a restricted case the caller can't read: only its id, state and times are shown.",
    )
    held: bool = Field(
        default=False,
        description="An approved action a stop is holding: releasing the hold runs it.",
    )
    waiting: Optional[str] = Field(
        default=None,
        description="Why a pending action waits: no reader of its restricted case can approve it.",
    )


class ApprovalListResponse(BaseModel):
    count: int
    actions: List[PendingActionResponse] = Field(default_factory=list)


class ApprovalActionResult(BaseModel):
    action: PendingActionResponse
    resume_result: Optional[Dict[str, Any]] = None


class NeedsYouHypothesis(BaseModel):
    hypothesis_id: str = ""
    statement: str
    provenance: str = ""


class NeedsYouItem(BaseModel):
    kind: Literal["approval", "checkpoint", "recommendation", "parked", "open_case"] = (
        Field(
            description=(
                "recommendation: a pending action nothing in Vigil executes, answered by "
                "acting in your own tools and closing its case. "
                "parked: a hunt stopped at a limit, answered with Keep going on its case; "
                "its source_id is the run id. "
                "open_case: a demo-mode case that is not closed, answered by opening the case; "
                "its source_id is the case id."
            )
        )
    )
    source_id: str
    title: str
    reason: str
    created_at: str = Field(
        description="'' when nothing recorded when it started waiting."
    )
    reversibility: Literal["reversible", "irreversible"]
    case_id: Optional[str] = Field(
        default=None, description="The first of case_ids: the case that asked."
    )
    case_ids: List[str] = Field(
        default_factory=list,
        description="Every case this item is on. A proposal several cases made is one item listing them all.",
    )
    finding_id: Optional[str] = Field(
        default=None,
        description="The first stored alert the approval's evidence names, so an item with no case can open it.",
    )
    run_id: Optional[str] = None
    checkpoint_id: Optional[str] = Field(
        default=None,
        description="The run's own checkpoint this item answers, so a page reading the run can tell it was answered.",
    )
    checkpoint_class: Optional[str] = None
    hypotheses: List[NeedsYouHypothesis] = Field(
        default_factory=list,
        description="A hypothesis approval's explanations, each with where it came from; base_rate is the benign one to beat.",
    )
    tool: Optional[str] = Field(
        default=None, description="The gated tool call this approval releases."
    )
    args: Optional[str] = Field(
        default=None,
        description="That call's arguments, verbatim: approving releases exactly these.",
    )
    containment: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "An isolation or account disable: the vendor IDs its target resolved "
            "to (candidates), or which tool is missing (missing)."
        ),
    )
    restricted: bool = Field(
        default=False,
        description="An item on a restricted case the caller can't read: no case, run or content.",
    )
    waiting: Optional[str] = Field(
        default=None,
        description="Why it waits: no reader of its restricted case can approve it.",
    )


class NeedsYouResponse(BaseModel):
    count: int
    items: List[NeedsYouItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _person(user: User) -> Claims:
    """The signed-in person, as a containment's call runs."""
    principal_id = getattr(user, "principal_id", None) or getattr(user, "user_id", None)
    return Claims(
        username=user.username,
        person_id=principal_id and str(principal_id),
        agent_id=None,
        run_id=None,
        origin="person",
    )


@router.get("/approvals", response_model=ApprovalListResponse)
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
    case_id: Optional[str] = Query(
        default=None,
        description=(
            "Only actions on this case, among the newest ``limit``. Requested "
            "containments are read from the vendor again first."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
    service: ApprovalService = Depends(provide_approvals),
    registry: MCPRegistry = Depends(provide_mcp_registry),
):
    """List approval actions, newest first."""
    from core.response.approval_service import ActionStatus

    status_enum: Optional[ActionStatus] = None
    if status:
        try:
            status_enum = ActionStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    if case_id:
        await containment.refresh_open(await containment.live_registry(registry))
    listed = service.list_actions(
        status=status_enum,
        workflow_run_id=workflow_run_id,
        limit=limit,
    )
    if case_id:
        listed = on_case(listed, case_id)
    actions = screen_actions(listed)
    if case_id:
        # Asked by case, a case the caller can't read has nothing to show.
        actions = [a for a in actions if not a.restricted]
    return {
        "count": len(actions),
        "actions": [asdict(a) for a in actions],
    }


@router.get("/approvals/pending")
async def list_pending_approvals(
    service: ApprovalService = Depends(provide_approvals),
) -> Dict[str, List[Dict[str, Any]]]:
    """Shortcut: only actions with ``status=pending`` and
    ``requires_approval=True``. Used by the AI Decisions approvals tab."""
    actions = screen_actions(service.list_pending_approvals())
    return {"actions": [asdict(a) for a in actions]}


@router.get("/approvals/needs-you", response_model=NeedsYouResponse)
async def list_needs_you(
    case_id: Optional[str] = Query(
        default=None,
        description="Only items whose resolved case id is this one.",
    ),
    registry: MCPRegistry = Depends(provide_mcp_registry),
):
    """Pending approvals and checkpoints that need a person, oldest first.

    Declared before ``/approvals/{action_id}`` so the literal path is not
    read as an action id. Uncapped: the decisions list stops at 500.
    Containments are resolved, and requested ones read again, first.
    """
    await containment.refresh_open(await containment.live_registry(registry))
    return needs_you(case_id)


@router.get("/approvals/{action_id}", response_model=PendingActionResponse)
async def get_approval(
    action_id: str,
    service: ApprovalService = Depends(provide_approvals),
    registry: MCPRegistry = Depends(provide_mcp_registry),
):
    """Fetch a single approval action, resolving or re-reading a containment."""
    action = await containment.on_read(
        service.get_action(action_id), await containment.live_registry(registry)
    )
    action = screen_action(action)
    if action is None:
        raise HTTPException(status_code=404, detail=f"Approval not found: {action_id}")
    return asdict(action)


@router.post(
    "/approvals/{action_id}/approve",
    dependencies=_DECIDE,
    response_model=ApprovalActionResult,
)
async def approve_action(
    action_id: str,
    request: ApproveRequest,
    service: ApprovalService = Depends(provide_approvals),
    current_user: User = Depends(get_current_user),
    registry: MCPRegistry = Depends(provide_mcp_registry),
):
    """Approve a pending action.

    If the action is linked to a paused workflow run, the run resumes
    automatically and the resume result is included in the response. An
    isolation or account disable names a resolved candidate (``candidate``);
    its call runs at once, as the approver, and the action returned says what
    the vendor answered. A failed one may be approved again.
    """
    from core.workflows.run_resume import resume_run

    action = service.get_action(action_id)
    if action is None:
        raise HTTPException(status_code=404, detail=f"Approval not found: {action_id}")

    # The body field is kept for the frozen contract; the record names the session user.
    approved_by = current_user.username
    principal_id = getattr(current_user, "principal_id", None) or getattr(
        current_user, "user_id", None
    )
    if (action.parameters or {}).get("undoes"):
        # A lift is run again with Release or Enable, which re-checks the original.
        raise HTTPException(
            status_code=409,
            detail=f"{action_id} undoes {action.parameters['undoes']}: use its undo",
        )
    call = None
    # Pending, failed, or approved with a run claim that outlived its process.
    if action.action_type in CONTAINMENT_TYPES and action.status in (
        "pending",
        "failed",
        "approved",
    ):
        if screen_action(action).restricted:
            raise HTTPException(
                status_code=404, detail=f"Approval not found: {action_id}"
            )
        registry = await containment.live_registry(registry)
        action = await containment.resolve(action, registry)
        try:
            call = containment.choose(action, request.candidate)
        except containment.Unapprovable as e:
            raise HTTPException(status_code=409, detail=str(e))
    try:
        updated = service.approve_action(
            action_id,
            approved_by=approved_by,
            approved_by_principal_id=principal_id,
            call=call,
        )
    except AlreadyDecided as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ActionNotFound:
        raise HTTPException(status_code=404, detail=f"Approval not found: {action_id}")
    except FourEyesRefused as e:
        raise HTTPException(status_code=403, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=500, detail="Failed to approve action")
    record_decision(updated, approved_by)
    await rollout_decided(updated, approved_by)
    if call is not None:
        updated = await containment.carry_out(updated, _person(current_user), registry)

    response: Dict[str, Any] = {
        "action": asdict(updated),
        "resume_result": None,
    }

    if updated.workflow_run_id:
        response["resume_result"] = await resume_run(
            updated.workflow_run_id, action_id, approved_by
        )

    return response


@router.post(
    "/approvals/{action_id}/undo",
    dependencies=_DECIDE,
    response_model=ApprovalActionResult,
)
async def undo_action(
    action_id: str,
    request: Optional[UndoRequest] = Body(default=None),
    service: ApprovalService = Depends(provide_approvals),
    current_user: User = Depends(get_current_user),
    registry: MCPRegistry = Depends(provide_mcp_registry),
):
    """Release an executed isolation, or enable an executed account disable.

    Inserts a row linked by ``parameters.undoes``, approved by the caller, that
    carries the vendor's undo tool on the same vendor ID, and runs it at once as
    the caller; a stop holds it. Four-eyes does not apply: no proposal is
    involved. The action returned is that row. The original records ``released``
    or ``enabled`` once the vendor reports it. 409 when the original is not
    executed and contained or disabled, or a lift of it is still open. An
    optional ``reason`` goes on the row and into an analyst note on the target,
    and the undo holds that act there for runs no person started (#5273).
    """
    action = service.get_action(action_id)
    if action is None or screen_action(action).restricted:
        raise HTTPException(status_code=404, detail=f"Approval not found: {action_id}")
    try:
        undone = await containment.undo(
            action_id,
            _person(current_user),
            registry,
            request.reason if request else None,
        )
    except NotUndoable as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ActionNotFound:
        raise HTTPException(status_code=404, detail=f"Approval not found: {action_id}")
    return {"action": asdict(undone), "resume_result": None}


@router.post(
    "/approvals/{action_id}/reject",
    dependencies=_DECIDE,
    response_model=ApprovalActionResult,
)
async def reject_action(
    action_id: str,
    request: RejectRequest,
    service: ApprovalService = Depends(provide_approvals),
    current_user: User = Depends(get_current_user),
):
    """Reject a pending action.

    If the action is linked to a paused workflow run, the run is
    cancelled with the supplied reason.
    """
    try:
        decided = await decide_reject(
            service,
            action_id,
            request.reason,
            current_user.username,
            getattr(current_user, "principal_id", None)
            or getattr(current_user, "user_id", None),
        )
    except AlreadyDecided as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ActionNotFound:
        raise HTTPException(status_code=404, detail=f"Approval not found: {action_id}")
    if decided is None:
        raise HTTPException(status_code=500, detail="Failed to reject action")
    updated, resume_result = decided
    return {"action": asdict(updated), "resume_result": resume_result}
