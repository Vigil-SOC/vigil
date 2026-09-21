"""Workflows API endpoints for SOC workflow management and execution."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.agents.projections import read_projection, read_replay
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
from core.workflows import catalog
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

# Rewrites in flight. One press is a whole model call over a run's record, and the two
# an impatient operator makes race to append to the same ledger. Per process, which is
# what a second worker behind a load balancer would slip past -- it bounds the common
# case (one person, one console) without a lock nobody else here takes.
_narrating: set[str] = set()


# -----------------------------------------------------------------------------
# Pydantic schemas
# -----------------------------------------------------------------------------


class WorkflowExecuteRequest(BaseModel):
    """Request to execute a workflow."""

    finding_id: Optional[str] = None
    case_id: Optional[str] = None
    context: Optional[str] = None
    hypothesis: Optional[str] = None
    # What each stated claim is about, keyed by the claim itself. Nothing here is
    # inferred: `host`, `user` and `process` have no shape a reader could find in a
    # sentence, so a subject is declared or a Verdict is recalled by nobody.
    hypothesis_subjects: Optional[Dict[str, List[str]]] = None
    # Turns, not model calls. Bounded so a typo cannot enqueue an hour of spend.
    iterations: Optional[int] = Field(default=None, ge=1, le=40)
    # What the caller will spend on this question, which is not a property of the
    # definition. Bounded because a mistyped ceiling is money.
    max_cost_usd: Optional[float] = Field(default=None, gt=0, le=100)
    # Whether the hunt stops and asks before it spends. The policy defaults to auto,
    # so a headless run advances with nobody at a terminal.
    approve_hypotheses: Optional[bool] = None


class HuntCoverageRequest(BaseModel):
    """A threat report and/or what was already extracted from it (#903)."""

    report: Optional[str] = None
    entity_keys: List[str] = Field(default_factory=list)
    techniques: List[str] = Field(default_factory=list)


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


# The catalog reads are the frozen contract, served at /api/v1/workflows by
# core/api/v1/workflows_router.py. These two routes keep the pre-version
# /api/workflows paths working by reading the same core.workflows.catalog
# functions. They must stay in THIS router so first-match order with
# /workflows/custom (which looks like a {workflow_id}) is decided by decorator
# order, not cross-router mount order.
@router.get("/workflows")
async def list_workflows(service: WorkflowsService = Depends(provide_workflows)):
    """List all available workflows."""
    return catalog.listing(service)


# Static routes MUST come before parameterized {workflow_id} routes
@router.post("/workflows/reload")
async def reload_workflows(service: WorkflowsService = Depends(provide_workflows)):
    """
    Force reload all file-based workflows from disk.

    Does not affect database-backed custom workflows.
    """
    service.reload()
    workflows = service.list_workflows()

    return {
        "success": True,
        "message": f"Reloaded workflows (total={len(workflows)})",
        "count": len(workflows),
    }


# -----------------------------------------------------------------------------
# Custom workflow CRUD (database-backed)
# -----------------------------------------------------------------------------


@router.get("/workflows/custom")
async def list_custom_workflows(
    active_only: bool = True,
    service: CustomWorkflowService = Depends(provide_custom_workflows),
):
    """List database-backed custom workflows."""
    rows = service.list(active_only=active_only)
    return {"workflows": rows, "count": len(rows)}


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
    wf = service.get(workflow_id)
    if not wf:
        raise HTTPException(
            status_code=404,
            detail=f"Custom workflow not found: {workflow_id}",
        )
    return wf


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
    ok = service.delete(workflow_id)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail=f"Custom workflow not found: {workflow_id}",
        )
    return {"success": True, "workflow_id": workflow_id}


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
    result = await generator.generate(payload.description)
    if not result.get("success"):
        raise HTTPException(
            status_code=502,
            detail=result.get("error") or "Workflow generation failed",
        )
    return {"draft": result["draft"]}


# -----------------------------------------------------------------------------
# Hunt coverage (#903)
# -----------------------------------------------------------------------------


@router.post("/workflows/threat-hunt/coverage")
async def check_hunt_coverage(payload: HuntCoverageRequest):
    """Say whether a threat report is already hunted: ``running``, ``concluded``
    or ``uncovered``. Read-only -- the caller decides whether to POST the
    returned ``proposal`` to ``/workflows/threat-hunt/execute``.

    The same function as the ``check_hunt_coverage`` agent tool, imported here
    so the router does not pull a database session factory in at import.
    """
    from core.memory.hunt_coverage import check_coverage

    try:
        return check_coverage(
            report=payload.report,
            entity_keys=payload.entity_keys,
            techniques=payload.techniques,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/workflows/threat-hunt/feed-proposals")
async def propose_feed_hunts(limit: int = 200):
    """Recent feed indicators nobody has hunted, each with a ``proposal`` body
    for ``/workflows/threat-hunt/execute`` (#905). Read-only, like the
    coverage route above and the ``propose_feed_hunts`` agent tool.
    """
    from core.threat_intel.threat_feed_service import (
        propose_hunts_from_recent_indicators,
    )

    return propose_hunts_from_recent_indicators(limit=limit)


# -----------------------------------------------------------------------------
# Parameterized discovery/execution routes (keep at bottom so specific paths
# like /workflows/custom and /workflows/reload match first)
# -----------------------------------------------------------------------------


@router.get("/workflows/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    service: WorkflowsService = Depends(provide_workflows),
    registry=Depends(provide_mcp_registry),
):
    """Get one workflow.

    Defined after /workflows/custom so decorator order resolves the {workflow_id}
    vs /custom ambiguity within this router.
    """
    workflow = catalog.detail(service, registry, workflow_id)
    if workflow is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow not found: {workflow_id}",
        )
    return workflow


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
    result = await service.execute_workflow(workflow_id, parameters, triggered_by="api")

    if not result.get("success"):
        error = result.get("error", "Unknown error during workflow execution")
        raise HTTPException(status_code=500, detail=error)

    return result


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
    if catalog.is_hunt(workflows, row.get("workflow_id")):
        row["hunt"] = await read_projection(run_id)
    return row


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
    if run_id in _narrating:
        raise HTTPException(
            status_code=409,
            detail="This run is already being written up. Reopen it when that finishes.",
        )
    _narrating.add(run_id)
    try:
        narrative = await write_narrative(run_id)
    except Exception as exc:  # noqa: BLE001 — the operator is owed the reason
        logger.error("could not write up run %s: %s", run_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from None
    finally:
        _narrating.discard(run_id)

    await _restate_summary(run_id, run_service)
    return {"success": True, "narrative": narrative}


@router.get("/workflows/runs/{run_id}/replay")
async def replay_workflow_run(
    run_id: str,
    decision_id: Optional[str] = None,
    run_service: WorkflowRunService = Depends(provide_workflow_runs),
):
    """Rebuild what each decision of a hunt was shown and compare it to the record.

    Not part of the polled run detail: this folds the whole ledger on the agent
    side, so it is answered only when an operator asks. Serve decides what is
    hunt-like; a run with nothing to replay is a 404 here too.
    """
    if not run_service.get_run(run_id):
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    try:
        report = await read_replay(run_id, decision_id)
    except Exception as exc:  # noqa: BLE001 — the operator is owed the reason
        logger.error("could not replay run %s: %s", run_id, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from None
    if report is None:
        raise HTTPException(
            status_code=404, detail=f"Nothing to replay for run: {run_id}"
        )
    return report


# result_summary was rendered with the account this rewrite supersedes. The console
# reads the projection and would show the new one either way, but the row is what an
# export and the case note the run filed both read, so leaving it makes two accounts
# of one hunt. Best effort: the account is written and journaled whatever happens here.
async def _restate_summary(run_id: str, run_service: WorkflowRunService) -> None:
    try:
        projection = await read_projection(run_id) or {}
        restated = projection.get("report_markdown")
        if restated:
            run_service.set_result_summary(run_id, restated)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not restate the stored summary of %s: %s", run_id, exc)


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
