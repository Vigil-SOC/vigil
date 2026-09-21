# Start an agent run and report its outcome. POST enqueues plain JSON and writes
# nothing; GET makes only the two reads Python is permitted against agent_events.

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from core.agents.directives import (
    DIRECTIVE_FIELDS,
    DIRECTIVE_KINDS,
    InvalidDirective,
    RunAlreadyEnded,
    UnknownRun,
    enqueue_directive,
)
from core.agents.queue import (
    RUN_KINDS,
    build_start_job,
    enqueue_run,
    new_run_id,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/agent-runs",
    tags=["agent-runs"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)


class StartRunRequest(BaseModel):
    run_kind: str = Field(default="hunt", description=f"One of {', '.join(RUN_KINDS)}.")
    arch: str = Field(
        default="",
        description="Arch file path; empty routes through the run-kind registry.",
    )
    playbook: str = Field(
        ..., description="Path to the playbook: the scenario as data."
    )
    config: str = Field(..., description="Path to the deployment config.")
    prompt: str = Field(default="", description="What the run is being asked to do.")
    overrides: Optional[Dict[str, Any]] = None
    tenant_id: Optional[str] = None


class StartRunResponse(BaseModel):
    run_id: str
    job_id: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str = Field(..., description="queued, running or terminal.")
    events: int = Field(
        ..., description="Events on the ledger, so progress is visible."
    )
    outcome: Optional[str] = None
    reason: Optional[str] = None


# Mint a run id and enqueue it. The worker opens the ledger, not this call.
@router.post("", response_model=StartRunResponse, status_code=202)
async def start_run(request: StartRunRequest) -> StartRunResponse:
    if request.run_kind not in RUN_KINDS:
        raise HTTPException(
            status_code=400, detail=f"unknown run_kind: {request.run_kind}"
        )

    run_id = new_run_id()
    payload: Dict[str, Any] = {
        "arch": request.arch,
        "playbook": request.playbook,
        "config": request.config,
        "prompt": request.prompt,
    }
    if request.overrides is not None:
        payload["overrides"] = request.overrides

    # approval_actions.workflow_run_id references workflow_runs, so a run with no
    # row there cannot raise an answerable checkpoint: the announce 500s and the
    # parked run waits out max_park_ms with nobody able to see it. Best-effort,
    # like every other write to that table -- the ledger is the record.
    _begin_run_row(run_id, request)

    job = build_start_job(
        run_id=run_id,
        run_kind=request.run_kind,
        request=payload,
        enqueued_by="api",
        tenant_id=request.tenant_id,
    )
    try:
        job_id = await enqueue_run(job)
    except Exception as exc:  # the queue is the only thing this endpoint can fail on
        logger.error("failed to enqueue agent run %s: %s", run_id, exc)
        raise HTTPException(status_code=503, detail="run queue unavailable") from exc

    return StartRunResponse(run_id=run_id, job_id=job_id)


# The playbook reference names the workflow when there is one; a run started from
# file paths is named for the loop it runs, which is all the console needs to list it.
def _begin_run_row(run_id: str, request: StartRunRequest) -> None:
    from core.workflows.workflow_run_service import WorkflowRunService

    # The scheme the agent layer resolves against /internal/playbooks
    # (services/agent/core/playbooks.ts::WORKFLOW_SCHEME).
    scheme = "workflow:"
    named = request.playbook.removeprefix(scheme).strip()
    workflow_id = named if request.playbook.startswith(scheme) else request.run_kind
    WorkflowRunService().begin_run(
        workflow_id=workflow_id,
        workflow_name=workflow_id,
        workflow_source="agent",
        trigger_context={"run_kind": request.run_kind, "prompt": request.prompt},
        triggered_by="api",
        run_id=run_id,
    )


def _has_run_row(session: Any, run_id: str) -> bool:
    row = session.execute(
        text("SELECT 1 FROM workflow_runs WHERE run_id = :run_id"),
        {"run_id": run_id},
    ).one_or_none()
    return row is not None


# Reports from state the worker persisted, using only the two permitted reads
# against agent_events; workflow_runs says whether the run was accepted at all.
@router.get("/{run_id}", response_model=RunStatusResponse)
def get_run(run_id: str, session: UnitOfWorkSession) -> RunStatusResponse:
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}") from None

    counted = session.execute(
        text(
            "SELECT count(*) AS events FROM agent_events WHERE run_id = CAST(:run_id AS uuid)"
        ),
        {"run_id": run_id},
    ).one_or_none()
    events = int(counted.events) if counted is not None else 0
    if events == 0:
        # Only the worker writes agent_events; POST wrote workflow_runs. A run with
        # that row and no events is accepted but not picked up yet, not unknown.
        if _has_run_row(session, run_id):
            return RunStatusResponse(run_id=run_id, status="queued", events=0)
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")

    terminal = session.execute(
        text(
            "SELECT payload FROM agent_events "
            "WHERE run_id = CAST(:run_id AS uuid) AND kind = 'terminal' ORDER BY seq LIMIT 1"
        ),
        {"run_id": run_id},
    ).one_or_none()
    if terminal is None:
        return RunStatusResponse(run_id=run_id, status="running", events=events)

    payload = terminal.payload
    return RunStatusResponse(
        run_id=run_id,
        status="terminal",
        events=events,
        outcome=payload.get("outcome"),
        reason=payload.get("reason"),
    )


class DirectiveRequest(BaseModel):
    kind: str = Field(..., description=f"One of {', '.join(DIRECTIVE_KINDS)}.")
    text: str = Field(default="", description="What the operator is telling the run.")
    actor: Optional[str] = Field(
        default=None, description="Who is steering. Defaults to the session user."
    )
    fields: Optional[Dict[str, Any]] = Field(
        default=None, description=f"Any of {', '.join(DIRECTIVE_FIELDS)}."
    )


class DirectiveResponse(BaseModel):
    directive_id: str
    kind: str
    created_at: str


# Steer a run that is already going. It queues rather than journals: the run
# holding the ledger is what turns a directive into a ledger event.
@router.post("/{run_id}/directives", response_model=DirectiveResponse, status_code=202)
def queue_directive(
    run_id: str, body: DirectiveRequest, session: UnitOfWorkSession
) -> DirectiveResponse:
    try:
        directive = enqueue_directive(
            session,
            run_id=run_id,
            kind=body.kind,
            body=body.text,
            actor=body.actor or "analyst",
            fields=body.fields,
        )
    except UnknownRun as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except RunAlreadyEnded as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except InvalidDirective as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return DirectiveResponse(
        directive_id=directive["directive_id"],
        kind=directive["kind"],
        created_at=directive["created_at"],
    )
