"""Day 1 Report: the latest record, Home's line, an on-demand run, and module switches.

The page is read under ``platform.read``. The visibility module is included
only when the caller holds ``integrations.read``, and coverage only with
``detections.read``. Each settings check is included only when the caller
holds that check's permission. The stored record stays complete. Running one,
and turning a module on or off, takes ``platform.write``.
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
from core.reporting.checks import MODULES
from core.reporting.day1 import (
    ON_DEMAND,
    begin_on_demand,
    finish,
    notify_finished,
    read_record,
    status,
)
from core.reporting.enablement import disabled_module_ids, set_module_enabled
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
    comparison: Optional[str] = None
    screen: Optional[str] = None
    prerequisites: List[str] = Field(default_factory=list)
    coverage: Optional[str] = None
    # When a coverage check was stored, or when a host-count read finished.
    # The section shows it as the result's age.
    checked_at: Optional[str] = None
    # Host-count integers. Unset when that read did not cover the list.
    listed: Optional[int] = None
    days_7_to_30: Optional[int] = None
    over_30_days: Optional[int] = None
    never_connected: Optional[int] = None
    degraded: Optional[int] = None


class Day1Comparison(BaseModel):
    kind: Literal["new", "fixed", "unchanged", "not_in_compared_run", "named"]
    prior_outcome: Optional[
        Literal["pass", "fail", "not_checked", "needs_a_person"]
    ] = None
    prior_evidence: Optional[Day1Evidence] = None


class Day1Check(BaseModel):
    id: str
    module: str
    outcome: Literal["pass", "fail", "not_checked", "needs_a_person"]
    reason: str
    evidence: Day1Evidence = Field(default_factory=Day1Evidence)
    comparison: Optional[Day1Comparison] = None
    cited_value: Optional[str] = None
    source_id: Optional[str] = None
    clause: Optional[str] = None
    link: Optional[str] = None
    date_checked: Optional[str] = None


class Day1Listed(BaseModel):
    source_id: str
    brings: str


class HistoryWindow(BaseModel):
    source: str
    start: str
    end: str
    outside: int


class HistoryImportRead(BaseModel):
    id: str
    source: str
    imported_at: str
    has_ticket_detail: bool
    included: bool
    note: Optional[str] = None
    left_out: List[str] = Field(default_factory=list)
    unmapped: List[str] = Field(default_factory=list)
    unmapped_note: Optional[str] = None
    unmapped_count: int = 0
    bulk_not_tested: Optional[int] = None


class HistoryFigure(BaseModel):
    label: str
    k: int
    n: int
    kind: Literal["measured", "estimated"]
    definition: str
    window_start: str
    window_end: str
    low: Optional[float] = None
    high: Optional[float] = None


class HistoryBand(BaseModel):
    label: str
    count: int


class HistoryTime(BaseModel):
    definition: str
    kind: Literal["measured", "estimated"]
    window_start: str
    window_end: str
    count: int
    opened_missing: int
    median: Optional[str] = None
    p90: Optional[str] = None
    bands: Optional[List[HistoryBand]] = None


class HistoryBulk(HistoryFigure):
    not_tested: List[str] = Field(default_factory=list)


class HistoryPerDay(BaseModel):
    count: int
    span_days: float
    per_day: Optional[float] = None
    kind: Literal["measured", "estimated"]
    definition: str
    window_start: str
    window_end: str


class HistorySeverity(BaseModel):
    severity: str
    closures: int
    dispositions: List[HistoryFigure] = Field(default_factory=list)
    time_to_close: List[HistoryTime] = Field(default_factory=list)
    bulk: HistoryBulk
    per_day: HistoryPerDay


class HistorySource(BaseModel):
    source: str
    window_start: str
    window_end: str
    outside: int
    closures: int
    dispositions: List[HistoryFigure] = Field(default_factory=list)
    time_to_close: List[HistoryTime] = Field(default_factory=list)
    bulk: HistoryBulk
    per_day: HistoryPerDay
    by_severity: List[HistorySeverity] = Field(default_factory=list)


class HistoryRecheckEntry(BaseModel):
    ticket: str
    source: str
    closed_at: str
    disposition: str
    severity: str
    reasons: List[str] = Field(default_factory=list)
    title: Optional[str] = None
    text_note: Optional[str] = None


class HistoryTuningEntry(BaseModel):
    group: Literal["rule", "title", "entity"]
    rule_id: Optional[str] = None
    ticket: Optional[str] = None
    label: Optional[str] = None
    entity_type: Optional[str] = None
    text_note: Optional[str] = None
    source: str
    window_start: str
    window_end: str
    judged: int
    true_positives: int
    low: Optional[float] = None
    high: Optional[float] = None
    kind: Literal["measured", "estimated"]
    definition: str
    where: str
    exclude_link: Optional[str] = None
    exclude_note: Optional[str] = None


class HistoryBefore(BaseModel):
    not_checked: Optional[str] = None
    how: Optional[str] = None
    notes: List[str] = Field(default_factory=list)
    imports: List[HistoryImportRead] = Field(default_factory=list)
    windows: List[HistoryWindow] = Field(default_factory=list)
    sources: List[HistorySource] = Field(default_factory=list)


class HistoryRecheck(BaseModel):
    not_checked: Optional[str] = None
    how: Optional[str] = None
    notes: List[str] = Field(default_factory=list)
    imports: List[HistoryImportRead] = Field(default_factory=list)
    windows: List[HistoryWindow] = Field(default_factory=list)
    total: Optional[int] = None
    entries: List[HistoryRecheckEntry] = Field(default_factory=list)


class HistoryTuning(BaseModel):
    not_checked: Optional[str] = None
    how: Optional[str] = None
    notes: List[str] = Field(default_factory=list)
    imports: List[HistoryImportRead] = Field(default_factory=list)
    windows: List[HistoryWindow] = Field(default_factory=list)
    candidates: List[HistoryTuningEntry] = Field(default_factory=list)


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
    compared_at: Optional[str] = None
    checks: List[Day1Check]
    switched_off: List[Day1Listed]
    history_before: Optional[HistoryBefore] = None
    history_recheck: Optional[HistoryRecheck] = None
    history_tuning: Optional[HistoryTuning] = None


class Day1Module(BaseModel):
    id: str
    enabled: bool


class Day1ModuleBody(BaseModel):
    enabled: bool


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


@router.get("/modules", response_model=List[Day1Module])
def day1_modules(
    _user: User = Depends(require_permission("platform.read")),
) -> List[Day1Module]:
    """Which report modules the next run includes. No row means every module."""
    disabled = disabled_module_ids()
    return [
        Day1Module(id=module_id, enabled=module_id not in disabled)
        for module_id in MODULES
    ]


@router.put("/modules/{module_id}", response_model=Day1Module)
def set_day1_module(
    module_id: str,
    body: Day1ModuleBody,
    user: User = Depends(require_permission("platform.write")),
) -> Day1Module:
    """Turn a report module on or off for the next run. The record stays as written."""
    if module_id not in MODULES:
        raise HTTPException(status_code=404, detail="Unknown report module.")
    saved = set_module_enabled(module_id, body.enabled, str(user.user_id))
    if not saved:
        raise HTTPException(status_code=500, detail="Could not save the report module.")
    return Day1Module(id=module_id, enabled=body.enabled)


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
