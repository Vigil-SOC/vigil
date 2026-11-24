"""Production Start Report: the latest record, Home's line, and an on-demand run.

Read under ``platform.read``. Running one takes ``platform.write``, and only
after a start is declared. The stored record stays complete; ``_present``
leaves out a module the caller cannot read.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.auth.permissions import require_permission
from core.deps import provide_process_health
from core.integrations.readiness import evaluate
from core.platform.process_health import ProcessHealth
from core.reporting.day1_router import Day1Check
from core.reporting.production import (
    PRODUCTION_ON_DEMAND,
    NotInProduction,
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
    prefix="/api/production-report",
    tags=["production-report"],
    auth=Auth.REQUIRED,
)


class ProductionReport(BaseModel):
    id: str
    run_id: str
    recorded_at: str
    started_at: str
    trigger: Literal["production", "production_on_demand"]
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    vigil_version: str
    preface: Optional[str] = None
    modules: List[str]
    withheld: List[str]
    # The Day 1 anchor the settings checks were read against. Nothing else
    # on this record is compared.
    compared_at: Optional[str] = None
    checks: List[Day1Check]
    evidence: Dict[str, Any] = Field(default_factory=dict)


class ProductionReportStatus(BaseModel):
    running_since: Optional[str] = None
    incomplete_since: Optional[str] = None
    latest_id: Optional[str] = None
    latest_at: Optional[str] = None
    latest_started_at: Optional[str] = None


def _report(user_id: str, record_id: Optional[str]) -> ProductionReport:
    body = read_record(user_id, record_id)
    if body is None:
        raise HTTPException(status_code=404, detail="No Production Start Report yet.")
    return ProductionReport.model_validate(body)


@router.get("/status", response_model=ProductionReportStatus)
def production_report_status(
    _user: User = Depends(require_permission("platform.read")),
) -> ProductionReportStatus:
    """Whether a production report is being written, failed to finish, or is ready."""
    return ProductionReportStatus.model_validate(status())


@router.get("", response_model=ProductionReport)
def production_report(
    record: Optional[str] = Query(None),
    user: User = Depends(require_permission("platform.read")),
) -> ProductionReport:
    """The latest Production Start Report, or the one named by ``record``."""
    return _report(user.user_id, record)


@router.post("", response_model=ProductionReport)
async def run_production_report(
    user: User = Depends(require_permission("platform.write")),
    processes: ProcessHealth = Depends(provide_process_health),
) -> ProductionReport:
    """Run the Production Start Report now. Tells the person who started it."""
    state = await evaluate(processes)
    try:
        run_id = await asyncio.to_thread(begin_on_demand, user.principal_id)
    except NotInProduction as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from None
    try:
        record_id = await asyncio.to_thread(
            finish, run_id, PRODUCTION_ON_DEMAND, user.principal_id, state
        )
    except Exception:  # noqa: BLE001 - the start stays, Home shows it did not complete
        logger.exception("on-demand production start report did not complete")
        raise HTTPException(status_code=500, detail="Internal server error") from None
    await asyncio.to_thread(
        notify_finished, PRODUCTION_ON_DEMAND, user.principal_id, record_id
    )
    return await asyncio.to_thread(_report, user.user_id, record_id)
