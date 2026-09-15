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

from core.cases.case_metrics_service import CaseMetricsService
from core.cases.case_sla_service import CaseSLAService
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import Case, CaseMetrics
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
async def get_dashboard(
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
async def get_sla_compliance(
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
async def get_analyst_performance(
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
async def get_mttr(
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
    from collections import defaultdict

    query = session.query(Case).filter(Case.status.in_(["resolved", "closed"]))

    if start_date:
        query = query.filter(Case.created_at >= start_date)
    if end_date:
        query = query.filter(Case.created_at <= end_date)
    if priority:
        query = query.filter(Case.priority == priority)

    cases = query.all()

    # Calculate MTTR (time from creation to resolution)
    mttr_by_priority = {}
    mttr_overall = []
    mttr_by_date = defaultdict(lambda: {"mttd": [], "mttr": []})

    for case in cases:
        metrics = (
            session.query(CaseMetrics)
            .filter(CaseMetrics.case_id == case.case_id)
            .first()
        )

        if metrics and metrics.time_to_resolve:
            mttr_overall.append(metrics.time_to_resolve)

            # By priority
            if case.priority not in mttr_by_priority:
                mttr_by_priority[case.priority] = []
            mttr_by_priority[case.priority].append(metrics.time_to_resolve)

            # By date (for trends)
            date_key = case.created_at.strftime("%Y-%m-%d")
            mttr_by_date[date_key]["mttr"].append(
                metrics.time_to_resolve / 3600
            )  # hours

            # Also add MTTD for trend comparison
            if metrics.time_to_respond:
                mttr_by_date[date_key]["mttd"].append(
                    metrics.time_to_respond / 3600
                )  # hours

    # Calculate averages
    avg_mttr = sum(mttr_overall) / len(mttr_overall) if mttr_overall else 0
    avg_by_priority = {}
    for pri, times in mttr_by_priority.items():
        avg_by_priority[pri] = sum(times) / len(times) if times else 0

    # Convert seconds to hours
    avg_mttr_hours = avg_mttr / 3600 if avg_mttr else 0
    avg_by_priority_hours = {k: v / 3600 for k, v in avg_by_priority.items()}

    # Prepare trend data
    trend_data = []
    for date, times in sorted(mttr_by_date.items()):
        trend_data.append(
            {
                "date": date,
                "mttd": sum(times["mttd"]) / len(times["mttd"]) if times["mttd"] else 0,
                "mttr": sum(times["mttr"]) / len(times["mttr"]) if times["mttr"] else 0,
            }
        )

    return {
        "average_mttr_seconds": avg_mttr,
        "average_mttr_hours": avg_mttr_hours,
        "mttr_by_priority": avg_by_priority_hours,
        "trend_data": trend_data,
        "total_cases": len(cases),
    }


@router.get("/velocity", **_BETA)
async def get_velocity(days: int = 30):
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
async def calculate_case_metrics(case_id: str):
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
async def get_breached_cases():
    """
    Get all cases with SLA breaches.

    Returns:
        List of breached cases
    """
    sla_service = CaseSLAService()
    breached = sla_service.get_breached_cases()
    return {"breached_cases": breached}


@router.get("/summary", response_model=CaseMetricsSummaryResponse)
async def get_summary(
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
async def get_mttd(
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

    query = session.query(Case)

    if start_date:
        query = query.filter(Case.created_at >= start_date)
    if end_date:
        query = query.filter(Case.created_at <= end_date)
    if priority:
        query = query.filter(Case.priority == priority)

    cases = query.all()

    # Calculate MTTD (time from creation to first response)
    mttd_by_priority = {}
    mttd_overall = []

    for case in cases:
        metrics = (
            session.query(CaseMetrics)
            .filter(CaseMetrics.case_id == case.case_id)
            .first()
        )

        if metrics and metrics.time_to_respond:
            mttd_overall.append(metrics.time_to_respond)

            if case.priority not in mttd_by_priority:
                mttd_by_priority[case.priority] = []
            mttd_by_priority[case.priority].append(metrics.time_to_respond)

    # Calculate averages
    avg_mttd = sum(mttd_overall) / len(mttd_overall) if mttd_overall else 0
    avg_by_priority = {}
    for pri, times in mttd_by_priority.items():
        avg_by_priority[pri] = sum(times) / len(times) if times else 0

    # Convert seconds to hours
    avg_mttd_hours = avg_mttd / 3600 if avg_mttd else 0
    avg_by_priority_hours = {k: v / 3600 for k, v in avg_by_priority.items()}

    return {
        "average_mttd_seconds": avg_mttd,
        "average_mttd_hours": avg_mttd_hours,
        "mttd_by_priority": avg_by_priority_hours,
        "total_cases": len(cases),
    }


@router.get("/by-priority", response_model=ByPriorityResponse)
async def get_by_priority(
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
async def get_by_status(
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
async def get_all_analyst_performance(
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

    query = session.query(Case)

    if start_date:
        query = query.filter(Case.created_at >= start_date)
    if end_date:
        query = query.filter(Case.created_at <= end_date)

    cases = query.all()

    # Group cases by analyst
    analyst_data = {}
    for case in cases:
        assignee = case.assignee or "unassigned"

        if assignee not in analyst_data:
            analyst_data[assignee] = {
                "analyst_id": assignee,
                "analyst_name": assignee,
                "cases_assigned": 0,
                "cases_resolved": 0,
                "avg_resolution_time": 0,
                "resolution_times": [],
            }

        analyst_data[assignee]["cases_assigned"] += 1

        if case.status in ["resolved", "closed"]:
            analyst_data[assignee]["cases_resolved"] += 1

            # Get resolution time
            metrics = (
                session.query(CaseMetrics)
                .filter(CaseMetrics.case_id == case.case_id)
                .first()
            )

            if metrics and metrics.time_to_resolve:
                analyst_data[assignee]["resolution_times"].append(
                    metrics.time_to_resolve / 3600  # Convert to hours
                )

    # Calculate averages
    analyst_performance = []
    for analyst_id, data in analyst_data.items():
        if data["resolution_times"]:
            data["avg_resolution_time"] = sum(data["resolution_times"]) / len(
                data["resolution_times"]
            )
        else:
            data["avg_resolution_time"] = 0

        # Remove temporary field
        del data["resolution_times"]

        analyst_performance.append(data)

    # Sort by cases assigned (descending)
    analyst_performance.sort(key=lambda x: x["cases_assigned"], reverse=True)

    return {"analyst_performance": analyst_performance}
