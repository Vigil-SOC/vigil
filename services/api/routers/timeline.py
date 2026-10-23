"""Timeline API endpoints for visualizing temporal security events."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from core.routing import Auth, RouterMeta
from core.storage.database_data_service import DatabaseDataService

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/timeline",
    tags=["timeline"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)


def normalize_timestamp(timestamp_str: str) -> datetime:
    """
    Normalize a timestamp string to a timezone-aware datetime object.

    Args:
        timestamp_str: ISO format timestamp string

    Returns:
        Timezone-aware datetime object
    """
    # Remove 'Z' and replace with '+00:00' for proper ISO parsing
    timestamp_str = timestamp_str.replace("Z", "+00:00")

    return _aware(datetime.fromisoformat(timestamp_str))


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _column_time(dt: datetime) -> datetime:
    """``dt`` as the naive UTC ``findings.timestamp`` holds, for a query bound.

    An aware bound is sent as ``timestamptz``, and comparing that to the naive
    column reads the column in the session's time zone rather than in UTC.
    """
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class TimelineEvent(BaseModel):
    """Timeline event model."""

    id: str
    content: str
    start: datetime
    end: Optional[datetime] = None
    type: str  # finding, activity, decision, status, note
    severity: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class TimelineResponse(BaseModel):
    """Timeline response model."""

    events: List[TimelineEvent]
    total: int
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None


@router.get("/range", response_model=TimelineResponse)
async def get_timeline_range(
    start: Optional[datetime] = Query(None, description="Start time (ISO format)"),
    end: Optional[datetime] = Query(None, description="End time (ISO format)"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    data_source: Optional[str] = Query(None, description="Filter by data source"),
    limit: int = Query(default=500, ge=1, le=5000),
):
    """
    Get timeline events for a specific time range.

    Args:
        start: Start time (ISO format)
        end: End time (ISO format)
        severity: Filter by severity
        data_source: Filter by data source
        limit: Maximum number of events

    Returns:
        Timeline events in the specified range
    """
    data_service = DatabaseDataService()

    start_time = _aware(start) if start else None
    end_time = _aware(end) if end else None

    # Undated findings are left out in the query rather than skipped after it:
    # timestamp DESC puts NULLs first in Postgres, so a page of them would
    # otherwise fill the limit and leave the dashboard's timeline empty. Findings
    # naming an analyst-excluded IP are left out too: this is the dashboard's
    # timeline, and it describes the queue. The range and the filters go into
    # the query too, so the limit counts matching findings: filtered after the
    # fetch, a window older than the newest ``limit`` findings came back empty.
    all_findings = data_service.get_findings(
        limit=limit,
        dated_only=True,
        exclusions="hide",
        severity=severity,
        data_source=data_source,
        timestamp_start=_column_time(start_time) if start_time else None,
        timestamp_end=_column_time(end_time) if end_time else None,
    )

    events: List[TimelineEvent] = []

    for finding in all_findings:
        # The demo data service ignores dated_only and the filters below, so they
        # are applied here as well; a finding with no time cannot be placed on a
        # timeline either way.
        if not finding.get("timestamp"):
            continue
        f_time = normalize_timestamp(finding["timestamp"])

        # Filter by time range if specified
        if start_time and f_time < start_time:
            continue
        if end_time and f_time > end_time:
            continue

        # Filter by severity if specified
        if severity and finding.get("severity") != severity:
            continue

        # Filter by data source if specified
        if data_source and finding.get("data_source") != data_source:
            continue

        events.append(
            TimelineEvent(
                id=f"finding-{finding['finding_id']}",
                content=f"Finding: {finding['finding_id']} - {finding.get('severity') or 'unknown'}",
                start=f_time,
                type="finding",
                severity=finding.get("severity"),
                metadata={
                    "finding_id": finding["finding_id"],
                    "data_source": finding.get("data_source"),
                    "anomaly_score": finding.get("anomaly_score"),
                    "entity_context": finding.get("entity_context"),
                },
            )
        )

    # Sort events by timestamp
    events.sort(key=lambda e: e.start)

    # Calculate actual time range from events
    actual_start = min(e.start for e in events) if events else start_time
    actual_end = max(e.start for e in events) if events else end_time

    return TimelineResponse(
        events=events,
        total=len(events),
        start_time=actual_start,
        end_time=actual_end,
    )
