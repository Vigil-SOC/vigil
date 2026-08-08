"""Agent runs API — start a run and report its outcome (GH #590).

Endpoints (registered under /api/agent-runs):
  POST   /            — mint a run id, enqueue it, return the id
  GET    /{run_id}    — report the run's status from what the worker persisted

The agent layer owns ``agent_events`` and is its single writer (ADR-0001), so
POST enqueues plain JSON and writes nothing. GET makes only the two reads the
Phase-0 contract permits Python: an existence/position check, and the terminal
event's payload. Anything richer would mean folding the ledger in a second
language, and the two folds would drift — that belongs behind an agent-layer API.
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from api._meta import Auth, RouterMeta

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from database.connection import get_db  # noqa: E402
from services.agent_queue import (  # noqa: E402
    RUN_KINDS,
    build_start_job,
    enqueue_run,
    new_run_id,
)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/agent-runs",
    tags=["agent-runs"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)


class StartRunRequest(BaseModel):
    run_kind: str = Field(default="hunt", description=f"One of {', '.join(RUN_KINDS)}.")
    arch: str = Field(..., description="Path to the arch file: the shape of the run.")
    playbook: str = Field(..., description="Path to the playbook: the scenario as data.")
    config: str = Field(..., description="Path to the deployment config.")
    prompt: str = Field(default="", description="What the run is being asked to do.")
    overrides: Optional[Dict[str, Any]] = None
    tenant_id: Optional[str] = None


class StartRunResponse(BaseModel):
    run_id: str
    job_id: str


class RunStatusResponse(BaseModel):
    run_id: str
    status: str = Field(..., description="running or terminal.")
    events: int = Field(..., description="Events on the ledger, so progress is visible.")
    outcome: Optional[str] = None
    reason: Optional[str] = None


@router.post("", response_model=StartRunResponse, status_code=202)
async def start_run(request: StartRunRequest) -> StartRunResponse:
    """Mint a run id and enqueue it. The worker opens the ledger, not this call."""
    if request.run_kind not in RUN_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown run_kind: {request.run_kind}")

    run_id = new_run_id()
    payload: Dict[str, Any] = {
        "arch": request.arch,
        "playbook": request.playbook,
        "config": request.config,
        "prompt": request.prompt,
    }
    if request.overrides is not None:
        payload["overrides"] = request.overrides

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


@router.get("/{run_id}", response_model=RunStatusResponse)
def get_run(run_id: str, db: Session = Depends(get_db)) -> RunStatusResponse:
    """Report the run from state the worker persisted, using only the two permitted reads."""
    try:
        uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}") from None

    counted = db.execute(
        text("SELECT count(*) AS events FROM agent_events WHERE run_id = CAST(:run_id AS uuid)"),
        {"run_id": run_id},
    ).one_or_none()
    events = int(counted.events) if counted is not None else 0
    if events == 0:
        raise HTTPException(status_code=404, detail=f"no such run: {run_id}")

    terminal = db.execute(
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
