"""Case metrics — versioned contract surface (``/api/v1/cases/metrics``).

Reporting numbers about cases (MTTR, MTTD, breach counts, breakdowns). Six are
frozen; six composite/rollup reads are marked beta via ``openapi_extra`` and are
excluded from the contract snapshot — their shape may change while the feature
matures. Beta still ships and returns data; it is a "do not rely on this yet"
label, not a hidden route.

Frozen: by-priority, by-status, breached, mttr, mttd, summary.
Beta:   dashboard, sla-compliance, velocity, analyst/{id}, analyst-performance,
        calculate/{id} (a recompute action, not a read).
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Float, and_
from sqlalchemy import case as sql_case
from sqlalchemy import cast, func, literal_column, select, true
from sqlalchemy.sql.elements import ColumnElement

from core.cases.case_metrics_service import CaseMetricsService
from core.cases.case_sla_service import CaseSLAService
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import Case, CaseClosureInfo
from core.storage.schemas import CaseMetricsSchema

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/v1/cases/metrics",
    tags=["case-metrics"],
    auth=Auth.REQUIRED,
    legacy_prefixes=("/api/cases/metrics",),
)

# Routes marked with this are in the versioned tree but NOT part of the frozen
# contract: composite rollups whose shape will change as reporting matures. The
# /api/v1/** contract snapshot excludes any operation carrying x-vigil-beta.
_BETA = {"openapi_extra": {"x-vigil-beta": True}}

# --- Derived timings ---------------------------------------------------------
# MTTR, MTTD and per-analyst resolution time are computed in SQL from columns
# every case path writes, not read from ``case_metrics``: nothing populates that
# table in normal operation (#1435), so reading it reported 0 for every
# deployment. The definitions are the ones ``CaseMetricsService`` uses to fill
# ``case_metrics``, so the two agree wherever both exist.

_CLOSED_STATUSES = ("resolved", "closed")

# A first-activity timestamp is only cast when it looks like an ISO-8601 date
# and time and Postgres accepts it as a ``timestamp`` (``pg_input_is_valid``,
# PG16+, which every shipped Postgres is), so one malformed entry -- "Feb 30",
# trailing junk -- cannot fail the whole aggregate. Writers store naive UTC,
# some with a trailing "Z"; a cast to ``timestamp`` ignores the zone suffix,
# which is what lines them up with the naive ``created_at``.
_ISO_DATETIME = (
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])[T ]([01]\d|2[0-3]):[0-5]\d"
)


def _closed_at() -> ColumnElement:
    """When a resolved or closed case was closed.

    ``case_closure_info.closed_at`` is stamped by ``close_case``, which every
    close path goes through, and it does not move when the case is edited
    afterwards. A ``resolved`` case has no closure row (and a reopened one may
    keep a stale one), so those fall back to ``cases.updated_at`` -- the source
    ``CaseMetricsService`` uses for ``time_to_resolve``.
    """
    return sql_case(
        (
            Case.status == "closed",
            func.coalesce(CaseClosureInfo.closed_at, Case.updated_at),
        ),
        else_=Case.updated_at,
    )


def _first_activity_at() -> ColumnElement:
    """The earliest timestamp in ``cases.activities``, or NULL if none."""
    activities = sql_case(
        (func.jsonb_typeof(Case.activities) == "array", Case.activities),
        else_=literal_column("'[]'::jsonb"),
    )
    entry = func.jsonb_array_elements(activities).table_valued("value")
    stamp = entry.c.value.op("->>")("timestamp")
    return (
        select(func.min(cast(stamp, Case.created_at.type)))
        .select_from(entry)
        .where(
            stamp.op("~")(_ISO_DATETIME),
            func.pg_input_is_valid(stamp, "timestamp"),
        )
        .scalar_subquery()
    )


def _seconds_since_created(moment: ColumnElement) -> ColumnElement:
    return cast(func.extract("epoch", moment - Case.created_at), Float)


def _created_window(
    start_date: Optional[datetime], end_date: Optional[datetime]
) -> List[ColumnElement]:
    filters: List[ColumnElement] = [true()]
    if start_date:
        filters.append(Case.created_at >= start_date)
    if end_date:
        filters.append(Case.created_at <= end_date)
    return filters


def _mean(sums: Dict[Any, List[float]], key: Any) -> Optional[float]:
    total, count = sums.get(key, (0.0, 0))
    return total / count if count else None


def _add(sums: Dict[Any, List[float]], key: Any, total: Any, count: int) -> None:
    pair = sums.setdefault(key, [0.0, 0])
    pair[0] += total or 0.0
    pair[1] += count


# --- Frozen response models -------------------------------------------------
# These six reads are the frozen contract, so their response shapes are pinned
# by the snapshot. Inner value types are kept permissive where the underlying
# service returns open maps; the envelope keys are the promise.


class MttrResponse(BaseModel):
    average_mttr_seconds: Optional[float] = None
    average_mttr_hours: Optional[float] = None
    mttr_by_priority: Dict[str, Optional[float]] = Field(default_factory=dict)
    trend_data: List[Dict[str, Any]] = Field(default_factory=list)
    total_cases: int


class MttdResponse(BaseModel):
    average_mttd_seconds: Optional[float] = None
    average_mttd_hours: Optional[float] = None
    mttd_by_priority: Dict[str, Optional[float]] = Field(default_factory=dict)
    total_cases: int


class CaseMetricsSummaryResponse(BaseModel):
    total_cases: int
    open_cases: int
    resolved_cases: int
    critical_cases: int
    status_breakdown: Dict[str, int] = Field(default_factory=dict)
    priority_breakdown: Dict[str, int] = Field(default_factory=dict)


class BreachedCasesResponse(BaseModel):
    breached_cases: List[Dict[str, Any]] = Field(default_factory=list)


class ByPriorityResponse(BaseModel):
    priority_breakdown: Dict[str, int] = Field(default_factory=dict)


class ByStatusResponse(BaseModel):
    status_breakdown: Dict[str, int] = Field(default_factory=dict)


metrics_service = CaseMetricsService()


@router.get("/dashboard", **_BETA)
def get_dashboard(
    start_date: Optional[datetime] = None, end_date: Optional[datetime] = None
):
    """
    Get dashboard metrics.

    Args:
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Dashboard metrics
    """
    metrics = metrics_service.get_dashboard_metrics(start_date, end_date)
    return metrics


@router.get("/sla-compliance", **_BETA)
def get_sla_compliance(
    start_date: Optional[datetime] = None, end_date: Optional[datetime] = None
):
    """
    Get SLA compliance report.

    Args:
        start_date: Start date filter
        end_date: End date filter

    Returns:
        SLA compliance statistics
    """
    sla_service = CaseSLAService()
    report = sla_service.get_sla_compliance_report(start_date, end_date)
    return report


@router.get("/analyst/{analyst_id}", **_BETA)
def get_analyst_performance(
    analyst_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    """
    Get analyst performance metrics.

    Args:
        analyst_id: Analyst user ID
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Analyst performance metrics
    """
    metrics = metrics_service.get_analyst_performance(analyst_id, start_date, end_date)
    return metrics


@router.get("/mttr", response_model=MttrResponse)
def get_mttr(
    session: UnitOfWorkSession,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    priority: Optional[str] = None,
):
    """
    Get Mean Time To Resolve metrics.

    Args:
        start_date: Start date filter
        end_date: End date filter
        priority: Filter by priority

    Returns:
        MTTR metrics by priority and trend data
    """
    resolve = _seconds_since_created(_closed_at())
    respond = _seconds_since_created(_first_activity_at())
    day = func.to_char(Case.created_at, "YYYY-MM-DD")

    filters = _created_window(start_date, end_date)
    filters.append(Case.status.in_(_CLOSED_STATUSES))
    if priority:
        filters.append(Case.priority == priority)

    # One grouped query. The overall, per-priority and per-day means are folded
    # from its sums and counts, so the query count does not grow with cases.
    rows = (
        session.query(
            Case.priority,
            day,
            func.count(),
            func.sum(resolve),
            func.count(resolve),
            func.sum(respond),
            func.count(respond),
        )
        .outerjoin(CaseClosureInfo, CaseClosureInfo.case_id == Case.case_id)
        .filter(and_(*filters))
        .group_by(Case.priority, day)
        .all()
    )

    total_cases = 0
    overall: Dict[Any, List[float]] = {}
    by_priority: Dict[Any, List[float]] = {}
    by_day_mttr: Dict[Any, List[float]] = {}
    by_day_mttd: Dict[Any, List[float]] = {}
    for pri, date_key, count, r_sum, r_count, d_sum, d_count in rows:
        total_cases += count
        if not r_count:
            continue
        _add(overall, None, r_sum, r_count)
        _add(by_priority, pri, r_sum, r_count)
        _add(by_day_mttr, date_key, r_sum, r_count)
        _add(by_day_mttd, date_key, d_sum, d_count)

    trend_data = []
    for date_key in sorted(by_day_mttr):
        day_mttr = _mean(by_day_mttr, date_key)
        day_mttd = _mean(by_day_mttd, date_key)
        trend_data.append(
            {
                "date": date_key,
                "mttd": day_mttd / 3600 if day_mttd is not None else 0,
                "mttr": day_mttr / 3600 if day_mttr is not None else 0,
            }
        )

    # Null, not 0, when no case in the window has a measured value: the
    # response model allows it, and 0 reads as "resolved instantly".
    avg_mttr = _mean(overall, None)
    return {
        "average_mttr_seconds": avg_mttr,
        "average_mttr_hours": avg_mttr / 3600 if avg_mttr is not None else None,
        "mttr_by_priority": {
            pri: total / count / 3600 for pri, (total, count) in by_priority.items()
        },
        "trend_data": trend_data,
        "total_cases": total_cases,
    }


@router.get("/velocity", **_BETA)
def get_velocity(days: int = 30):
    """
    Get case velocity (opened vs closed).

    Args:
        days: Number of days to analyze

    Returns:
        Velocity data
    """
    velocity = metrics_service.get_case_velocity(days)
    return velocity


@router.post("/calculate/{case_id}", **_BETA)
def calculate_case_metrics(case_id: str):
    """
    Calculate/update metrics for a case.

    Args:
        case_id: Case ID

    Returns:
        Calculated metrics
    """
    metrics = metrics_service.calculate_case_metrics(case_id)
    if not metrics:
        raise HTTPException(status_code=404, detail="Case not found")
    return CaseMetricsSchema.dump(metrics)


@router.get("/breached", response_model=BreachedCasesResponse)
def get_breached_cases():
    """
    Get all cases with SLA breaches.

    Returns:
        List of breached cases
    """
    sla_service = CaseSLAService()
    breached = sla_service.get_breached_cases()
    return {"breached_cases": breached}


@router.get("/summary", response_model=CaseMetricsSummaryResponse)
def get_summary(
    start_date: Optional[datetime] = None, end_date: Optional[datetime] = None
):
    """
    Get summary metrics for cases.

    Args:
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Summary metrics including total cases, open cases, etc.
    """
    metrics = metrics_service.get_dashboard_metrics(start_date, end_date)
    return {
        "total_cases": metrics.get("total_cases", 0),
        "open_cases": metrics.get("open_cases_count", 0),
        "resolved_cases": metrics.get("resolved_cases_count", 0),
        "critical_cases": metrics.get("priority_breakdown", {}).get("critical", 0),
        "status_breakdown": metrics.get("status_breakdown", {}),
        "priority_breakdown": metrics.get("priority_breakdown", {}),
    }


@router.get("/mttd", response_model=MttdResponse)
def get_mttd(
    session: UnitOfWorkSession,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    priority: Optional[str] = None,
):
    """
    Get Mean Time To Detect metrics.

    Args:
        start_date: Start date filter
        end_date: End date filter
        priority: Filter by priority

    Returns:
        MTTD metrics by priority
    """
    respond = _seconds_since_created(_first_activity_at())

    filters = _created_window(start_date, end_date)
    if priority:
        filters.append(Case.priority == priority)

    rows = (
        session.query(
            Case.priority, func.count(), func.sum(respond), func.count(respond)
        )
        .filter(and_(*filters))
        .group_by(Case.priority)
        .all()
    )

    total_cases = 0
    overall: Dict[Any, List[float]] = {}
    by_priority: Dict[Any, List[float]] = {}
    for pri, count, d_sum, d_count in rows:
        total_cases += count
        if d_count:
            _add(overall, None, d_sum, d_count)
            _add(by_priority, pri, d_sum, d_count)

    avg_mttd = _mean(overall, None)
    return {
        "average_mttd_seconds": avg_mttd,
        "average_mttd_hours": avg_mttd / 3600 if avg_mttd is not None else None,
        "mttd_by_priority": {
            pri: total / count / 3600 for pri, (total, count) in by_priority.items()
        },
        "total_cases": total_cases,
    }


@router.get("/by-priority", response_model=ByPriorityResponse)
def get_by_priority(
    session: UnitOfWorkSession,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    """
    Get case counts by priority.

    Args:
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Case counts broken down by priority
    """

    query = session.query(Case)

    if start_date:
        query = query.filter(Case.created_at >= start_date)
    if end_date:
        query = query.filter(Case.created_at <= end_date)

    cases = query.all()

    # Count by priority and status
    priority_data = {}
    for case in cases:
        priority = case.priority or "unknown"

        if priority not in priority_data:
            priority_data[priority] = {
                "priority": priority,
                "count": 0,
                "closed_count": 0,
            }

        priority_data[priority]["count"] += 1
        if case.status in ["resolved", "closed"]:
            priority_data[priority]["closed_count"] += 1

    # Sort by priority order
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
    priority_breakdown = sorted(
        priority_data.values(), key=lambda x: priority_order.get(x["priority"], 99)
    )

    return {"priority_breakdown": priority_breakdown}


@router.get("/by-status", response_model=ByStatusResponse)
def get_by_status(
    session: UnitOfWorkSession,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    """
    Get case counts by status.

    Args:
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Case counts broken down by status
    """

    query = session.query(Case)

    if start_date:
        query = query.filter(Case.created_at >= start_date)
    if end_date:
        query = query.filter(Case.created_at <= end_date)

    cases = query.all()

    # Count by status
    status_data = {}
    for case in cases:
        status = case.status or "unknown"

        if status not in status_data:
            status_data[status] = {"status": status, "count": 0}

        status_data[status]["count"] += 1

    status_breakdown = list(status_data.values())

    return {"status_breakdown": status_breakdown}


@router.get("/analyst-performance", **_BETA)
def get_all_analyst_performance(
    session: UnitOfWorkSession,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
):
    """
    Get performance metrics for all analysts.

    Args:
        start_date: Start date filter
        end_date: End date filter

    Returns:
        Performance metrics for all analysts
    """
    analyst = func.coalesce(func.nullif(Case.assignee, ""), "unassigned")
    is_closed = Case.status.in_(_CLOSED_STATUSES)
    resolve = sql_case((is_closed, _seconds_since_created(_closed_at())))

    rows = (
        session.query(
            analyst,
            func.count(),
            func.count(sql_case((is_closed, 1))),
            func.avg(resolve),
        )
        .outerjoin(CaseClosureInfo, CaseClosureInfo.case_id == Case.case_id)
        .filter(and_(*_created_window(start_date, end_date)))
        .group_by(analyst)
        .order_by(func.count().desc(), analyst)
        .all()
    )

    # avg_resolution_time stays 0 rather than null for an analyst with nothing
    # resolved: this beta route has no response model and the console types the
    # field as a number.
    analyst_performance = [
        {
            "analyst_id": name,
            "analyst_name": name,
            "cases_assigned": assigned,
            "cases_resolved": resolved,
            "avg_resolution_time": avg / 3600 if avg is not None else 0,
        }
        for name, assigned, resolved, avg in rows
    ]

    return {"analyst_performance": analyst_performance}
