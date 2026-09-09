"""Timeline API endpoints for visualizing temporal security events."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
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

    # Parse the timestamp
    dt = datetime.fromisoformat(timestamp_str)

    # If timezone-naive, assume UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


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


@router.get("/case/{case_id}", response_model=TimelineResponse)
async def get_case_timeline(case_id: str):
    """
    Get timeline events for a specific case.

    Includes:
    - Case timeline events
    - Associated findings
    - Activities
    - Status changes
    - Notes

    Args:
        case_id: Case identifier

    Returns:
        Timeline events for the case
    """
    data_service = DatabaseDataService()
    case = data_service.get_case(case_id)

    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    events: List[TimelineEvent] = []

    # Add case creation event
    events.append(
        TimelineEvent(
            id=f"case-created-{case_id}",
            content=f"Case created: {case.get('title', 'Untitled')}",
            start=normalize_timestamp(case["created_at"]),
            type="status",
            severity=case.get("priority"),
            metadata={"case_id": case_id, "action": "created"},
        )
    )

    # Add timeline events from case
    for idx, timeline_event in enumerate(case.get("timeline", [])):
        events.append(
            TimelineEvent(
                id=f"timeline-{case_id}-{idx}",
                content=timeline_event.get("event", "Event"),
                start=normalize_timestamp(timeline_event["timestamp"]),
                type="activity",
                metadata={"case_id": case_id, **timeline_event},
            )
        )

    # Add activities
    for idx, activity in enumerate(case.get("activities", [])):
        events.append(
            TimelineEvent(
                id=f"activity-{case_id}-{idx}",
                content=activity.get("description", "Activity"),
                start=normalize_timestamp(
                    activity.get("timestamp", case["created_at"])
                ),
                type="activity",
                metadata={"case_id": case_id, **activity},
            )
        )

    # Add notes as events
    for idx, note in enumerate(case.get("notes", [])):
        events.append(
            TimelineEvent(
                id=f"note-{case_id}-{idx}",
                content=f"Note: {note.get('content', '')[:100]}",
                start=normalize_timestamp(note.get("timestamp", case["created_at"])),
                type="note",
                metadata={
                    "case_id": case_id,
                    "author": note.get("author"),
                    "full_content": note.get("content"),
                },
            )
        )

    # Add findings as events
    findings = data_service.get_findings_by_case(case_id)
    for finding in findings:
        events.append(
            TimelineEvent(
                id=f"finding-{finding['finding_id']}",
                content=f"Finding: {finding['finding_id']} - {finding.get('severity', 'unknown')}",
                start=normalize_timestamp(finding["timestamp"]),
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

    # Calculate time range
    start_time = min(e.start for e in events) if events else None
    end_time = max(e.start for e in events) if events else None

    return TimelineResponse(
        events=events, total=len(events), start_time=start_time, end_time=end_time
    )


@router.get("/finding/{finding_id}/context", response_model=TimelineResponse)
async def get_finding_context_timeline(
    finding_id: str, time_window_minutes: int = Query(default=60, ge=1, le=1440)
):
    """
    Get timeline context around a specific finding.

    Shows related findings within a time window before and after the finding.

    Args:
        finding_id: Finding identifier
        time_window_minutes: Minutes before/after to include (default 60)

    Returns:
        Timeline events around the finding
    """
    data_service = DatabaseDataService()
    finding = data_service.get_finding(finding_id)

    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")

    finding_time = normalize_timestamp(finding["timestamp"])
    start_time = finding_time - timedelta(minutes=time_window_minutes)
    end_time = finding_time + timedelta(minutes=time_window_minutes)

    # Get findings in time window
    all_findings = data_service.get_findings(limit=1000)

    events: List[TimelineEvent] = []

    for f in all_findings:
        f_time = normalize_timestamp(f["timestamp"])
        if start_time <= f_time <= end_time:
            is_target = f["finding_id"] == finding_id
            events.append(
                TimelineEvent(
                    id=f"finding-{f['finding_id']}",
                    content=f"{'🎯 ' if is_target else ''}Finding: {f['finding_id']} - {f.get('severity', 'unknown')}",
                    start=f_time,
                    type="finding",
                    severity=f.get("severity"),
                    metadata={
                        "finding_id": f["finding_id"],
                        "data_source": f.get("data_source"),
                        "anomaly_score": f.get("anomaly_score"),
                        "entity_context": f.get("entity_context"),
                        "is_target": is_target,
                    },
                )
            )

    # Sort events by timestamp
    events.sort(key=lambda e: e.start)

    return TimelineResponse(
        events=events, total=len(events), start_time=start_time, end_time=end_time
    )


@router.get("/range", response_model=TimelineResponse)
async def get_timeline_range(
    start: Optional[str] = Query(None, description="Start time (ISO format)"),
    end: Optional[str] = Query(None, description="End time (ISO format)"),
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

    # Parse time range
    start_time = normalize_timestamp(start) if start else None
    end_time = normalize_timestamp(end) if end else None

    # Get findings
    all_findings = data_service.get_findings(limit=limit)

    events: List[TimelineEvent] = []

    for finding in all_findings:
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
                content=f"Finding: {finding['finding_id']} - {finding.get('severity', 'unknown')}",
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


@router.get("/cluster/{cluster_id}", response_model=TimelineResponse)
async def get_cluster_timeline(cluster_id: str):
    """
    Get timeline events for findings in a specific cluster.

    Args:
        cluster_id: Cluster identifier

    Returns:
        Timeline events for the cluster
    """
    data_service = DatabaseDataService()

    # Get findings in cluster
    all_findings = data_service.get_findings(limit=1000)

    # Filter by cluster_id
    findings = [f for f in all_findings if f.get("cluster_id") == cluster_id]

    if not findings:
        raise HTTPException(
            status_code=404, detail="Cluster not found or has no findings"
        )

    events: List[TimelineEvent] = []

    for finding in findings:
        events.append(
            TimelineEvent(
                id=f"finding-{finding['finding_id']}",
                content=f"Finding: {finding['finding_id']} - {finding.get('severity', 'unknown')}",
                start=normalize_timestamp(finding["timestamp"]),
                type="finding",
                severity=finding.get("severity"),
                metadata={
                    "finding_id": finding["finding_id"],
                    "data_source": finding.get("data_source"),
                    "anomaly_score": finding.get("anomaly_score"),
                    "entity_context": finding.get("entity_context"),
                    "cluster_id": cluster_id,
                },
            )
        )

    # Sort events by timestamp
    events.sort(key=lambda e: e.start)

    # Calculate time range
    start_time = min(e.start for e in events) if events else None
    end_time = max(e.start for e in events) if events else None

    return TimelineResponse(
        events=events, total=len(events), start_time=start_time, end_time=end_time
    )
