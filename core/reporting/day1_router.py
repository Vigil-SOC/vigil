"""Day 1 Report: the latest record, Home's line, and an on-demand run.

The page is read under ``platform.read``. The visibility module is included
only when the caller holds ``integrations.read``; the stored record stays
complete. Running one takes ``platform.write``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth.permissions import require_permission
from core.deps import provide_process_health
from core.integrations.readiness import evaluate
from core.platform.process_health import ProcessHealth
from core.reporting.day1 import (
    ON_DEMAND,
    begin_on_demand,
    finish,
    notify_finished,
    read_record,
    status,
)
from core.routing import Auth, RouterMeta
from core.storage.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/day1-report",
    tags=["day1-report"],
    auth=Auth.REQUIRED,
)


class Day1Evidence(BaseModel):
    fix: Optional[str] = None
    source_id: Optional[str] = None
    brings: Optional[str] = None
    state: Optional[str] = None
    last_data: Optional[str] = None
    labels: List[str] = Field(default_factory=list)


class Day1Check(BaseModel):
    id: str
    module: str
    outcome: Literal["pass", "fail", "not_checked", "needs_a_person"]
    reason: str
    evidence: Day1Evidence = Field(default_factory=Day1Evidence)


class Day1Listed(BaseModel):
    source_id: str
    brings: str


class Day1Report(BaseModel):
    id: str
    run_id: str
    recorded_at: str
    started_at: str
    trigger: Literal["readiness", "fallback", "on_demand"]
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    vigil_version: str
    preface: Optional[str] = None
    modules: List[str]
    withheld: List[str]
    checks: List[Day1Check]
    switched_off: List[Day1Listed]


class Day1Status(BaseModel):
    running_since: Optional[str] = None
    incomplete_since: Optional[str] = None
    latest_id: Optional[str] = None
    latest_at: Optional[str] = None


def _report(user_id: str, record_id: Optional[str]) -> Day1Report:
    body = read_record(user_id, record_id)
    if body is None:
        raise HTTPException(status_code=404, detail="No Day 1 Report yet.")
    return Day1Report.model_validate(body)


@router.get("/status", response_model=Day1Status)
def day1_status(
    _user: User = Depends(require_permission("platform.read")),
) -> Day1Status:
    """Whether a report is being written, failed to finish, or is ready to open."""
    return Day1Status.model_validate(status())


@router.get("", response_model=Day1Report)
def day1_report(
    record: Optional[str] = Query(None),
    user: User = Depends(require_permission("platform.read")),
) -> Day1Report:
    """The latest Day 1 Report, or the one named by ``record``."""
    return _report(user.user_id, record)


@router.post("", response_model=Day1Report)
async def run_day1_report(
    user: User = Depends(require_permission("platform.write")),
    processes: ProcessHealth = Depends(provide_process_health),
) -> Day1Report:
    """Run the Day 1 Report now. Tells the person who started it."""
    state = await evaluate(processes)
    run_id = await asyncio.to_thread(begin_on_demand, user.principal_id)
    try:
        record_id = await asyncio.to_thread(
            finish, run_id, ON_DEMAND, user.principal_id, state
        )
    except Exception:  # noqa: BLE001 - the start stays, Home shows it did not complete
        logger.exception("on-demand day 1 report did not complete")
        raise HTTPException(status_code=500, detail="Internal server error") from None
    await asyncio.to_thread(notify_finished, ON_DEMAND, user.principal_id, record_id)
    return await asyncio.to_thread(_report, user.user_id, record_id)
