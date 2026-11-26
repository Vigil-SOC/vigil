"""Day 1 Report: the latest record, Home's line, an on-demand run, and module switches.

The page is read under ``platform.read``. The visibility module is included
only when the caller holds ``integrations.read``, and coverage only with
``detections.read``. Each settings check is included only when the caller
holds that check's permission. The stored record stays complete. Running one,
turning a module on or off, and setting how often it runs again take
``platform.write``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.auth.permissions import require_permission
from core.reporting.checks import MODULES
from core.reporting.day1 import read_record, run_on_demand, status
from core.reporting.enablement import (
    RECURRING_MAX_DAYS,
    disabled_module_ids,
    recurring_days,
    set_module_enabled,
    set_recurring_days,
)
from core.reporting.forecast import Day1Forecast
from core.routing import Auth, RouterMeta
from core.storage.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

NO_REPORT = "No Day 1 Report yet."

ROUTER_META = RouterMeta(
    prefix="/api/day1-report",
    tags=["day1-report"],
    auth=Auth.REQUIRED,
)


class Day1Provider(BaseModel):
    """One provider type the model components reach. Never an address or key."""

    provider_type: str
    # The class from core.llm.residency; the console labels it.
    residency: str
    reason: Optional[str] = None
    components: List[str] = Field(default_factory=list)
    statements: List[str] = Field(default_factory=list)
    self_hosted_by: Optional[str] = None
    self_hosted_at: Optional[str] = None


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
    # Where model processing runs: the providers reached, and the unscored
    # CJIS sentence for counsel.
    providers: List[Day1Provider] = Field(default_factory=list)
    counsel: Optional[str] = None
    # When a coverage check was stored, or when a host-count read finished.
    # The section shows it as the result's age.
    checked_at: Optional[str] = None
    # Host-count integers. Unset when that read did not cover the list.
    listed: Optional[int] = None
    days_7_to_30: Optional[int] = None
    over_30_days: Optional[int] = None
    never_connected: Optional[int] = None
    degraded: Optional[int] = None
    # Site counts. Unset when that check was not checked.
    checked: Optional[int] = None
    unseen: Optional[int] = None
    outside: Optional[int] = None
    unowned: Optional[int] = None
    undeclared: Optional[int] = None


class Day1Comparison(BaseModel):
    kind: Literal["new", "fixed", "unchanged", "not_in_compared_run", "named"]
    prior_outcome: Optional[
        Literal["pass", "fail", "not_checked", "needs_a_person", "no_value"]
    ] = None
    prior_evidence: Optional[Day1Evidence] = None
    # Set when the outcome changed because a cited value or a kind moved.
    change: Optional[str] = None


class Day1Proposed(BaseModel):
    cited_value: str
    source_id: str
    scope: str


class Day1Check(BaseModel):
    id: str
    module: str
    outcome: Literal["pass", "fail", "not_checked", "needs_a_person", "no_value"]
    reason: str
    evidence: Day1Evidence = Field(default_factory=Day1Evidence)
    comparison: Optional[Day1Comparison] = None
    cited_value: Optional[str] = None
    source_id: Optional[str] = None
    clause: Optional[str] = None
    link: Optional[str] = None
    date_checked: Optional[str] = None
    scope: Optional[str] = None
    proposed: List[Day1Proposed] = Field(default_factory=list)
    gap: Optional[str] = None
    window: Optional[str] = None


class Day1Selection(BaseModel):
    jurisdictions: List[str] = Field(default_factory=list)
    kinds: List[str] = Field(default_factory=list)
    unreadable: Optional[str] = None


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


CappedGroupName = Literal["top", "control"]
CappedDirectionName = Literal[
    "not_real_not_real",
    "not_real_real",
    "undetermined_real",
    "undetermined_not_real",
    "not_real_undetermined",
    "undetermined_undetermined",
    "could_not",
]


class CappedDirection(BaseModel):
    direction: CappedDirectionName
    k: int
    low: Optional[float] = None
    high: Optional[float] = None


class CappedTicket(BaseModel):
    ticket: str
    source: str
    group: CappedGroupName
    direction: CappedDirectionName
    reason: Optional[str] = None
    title: Optional[str] = None
    entities: List[str] = Field(default_factory=list)
    text_note: Optional[str] = None


class CappedCost(BaseModel):
    runs: int = 0
    calls: Optional[int] = None
    wall_ms: Optional[int] = None
    dollars: Optional[float] = None
    priced_at_zero: bool = False
    local_model: bool = False
    unpriced: bool = False
    unread: bool = False


class CappedGroup(BaseModel):
    group: CappedGroupName
    n: int
    rows: List[CappedDirection] = Field(default_factory=list)
    after_close: int = 0
    approximate: int = 0
    tickets: List[CappedTicket] = Field(default_factory=list)


class CappedBatch(BaseModel):
    batch_id: str
    status: str
    groups: List[CappedGroup] = Field(default_factory=list)
    cost: CappedCost


class HistoryCappedRecheck(BaseModel):
    not_checked: Optional[str] = None
    how: Optional[str] = None
    awaiting_list: bool = False
    none_run: bool = False
    open_only: bool = False
    finished: Optional[CappedBatch] = None
    stopped: Optional[CappedBatch] = None


class LayerMove(BaseModel):
    technique: str
    name: Optional[str] = None
    layer: str
    kind: Literal["improved", "regressed"]
    before: str
    after: str


class LayerCounts(BaseModel):
    improved: int
    regressed: int
    unchanged: int
    not_compared: int


class LayersComparison(BaseModel):
    counts: LayerCounts
    moves: List[LayerMove] = Field(default_factory=list)


class Day1Report(BaseModel):
    id: str
    run_id: str
    recorded_at: str
    started_at: str
    trigger: Literal["readiness", "fallback", "on_demand", "recurring"]
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    vigil_version: str
    preface: Optional[str] = None
    modules: List[str]
    withheld: List[str]
    compared_at: Optional[str] = None
    # Modules the compared run had and this one did not run.
    not_run: List[str] = Field(default_factory=list)
    # A recurring run: a section the caller can't read got worse since the last run.
    hidden_worse: bool = False
    checks: List[Day1Check]
    selection: Optional[Day1Selection] = None
    # Modules the caller may read. Visibility, history and the forecast stay
    # on their own fields; this is the remainder.
    evidence: Dict[str, Any] = Field(default_factory=dict)
    switched_off: List[Day1Listed]
    history_before: Optional[HistoryBefore] = None
    history_recheck: Optional[HistoryRecheck] = None
    history_tuning: Optional[HistoryTuning] = None
    history_capped_recheck: Optional[HistoryCappedRecheck] = None
    forecast: Optional[Day1Forecast] = None
    # Coverage by layer against the compared run. Read, never stored.
    coverage_comparison: Union[
        LayersComparison, Literal["not_in_compared_run"], None
    ] = None


class Day1Module(BaseModel):
    id: str
    enabled: bool


class Day1ModuleBody(BaseModel):
    enabled: bool


class Day1Recurring(BaseModel):
    # Whole days between runs. 0 is off.
    days: int = Field(ge=0, le=RECURRING_MAX_DAYS)


class Day1Status(BaseModel):
    running_since: Optional[str] = None
    incomplete_since: Optional[str] = None
    latest_id: Optional[str] = None
    latest_at: Optional[str] = None


def presented(user_id: str, record_id: Optional[str]) -> Optional[dict[str, Any]]:
    """The record as GET /api/day1-report returns it, or None. /mcp reads it here too."""
    body = read_record(user_id, record_id)
    return (
        None
        if body is None
        else Day1Report.model_validate(body).model_dump(mode="json")
    )


def _report(user_id: str, record_id: Optional[str]) -> Day1Report:
    body = read_record(user_id, record_id)
    if body is None:
        raise HTTPException(status_code=404, detail=NO_REPORT)
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


@router.get("/recurring", response_model=Day1Recurring)
def day1_recurring(
    _user: User = Depends(require_permission("platform.read")),
) -> Day1Recurring:
    """How often the report runs again on its own. No row is every 7 days."""
    return Day1Recurring(days=recurring_days())


@router.put("/recurring", response_model=Day1Recurring)
def set_day1_recurring(
    body: Day1Recurring,
    user: User = Depends(require_permission("platform.write")),
) -> Day1Recurring:
    """Set the interval in whole days, or 0 to switch recurring runs off."""
    if not set_recurring_days(body.days, str(user.user_id)):
        raise HTTPException(status_code=500, detail="Could not save the interval.")
    return body


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
    request: Request,
    user: User = Depends(require_permission("platform.write")),
) -> Day1Report:
    """Run the Day 1 Report now. Tells the person who started it."""
    try:
        record_id = await run_on_demand(
            user.principal_id, request.app.state.process_health
        )
    except Exception:  # noqa: BLE001 - the start stays, Home shows it did not complete
        logger.exception("on-demand day 1 report did not complete")
        raise HTTPException(status_code=500, detail="Internal server error") from None
    return await asyncio.to_thread(_report, user.user_id, record_id)
