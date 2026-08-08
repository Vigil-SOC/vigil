"""Timeline API endpoints for visualizing temporal security events."""

from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import logging

from core.storage.database_data_service import DatabaseDataService
from core.routing import Auth, RouterMeta

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
    timestamp_str = timestamp_str.replace('Z', '+00:00')
    
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
    try:
        data_service = DatabaseDataService()
        case = data_service.get_case(case_id)
        
        if not case:
            raise HTTPException(status_code=404, detail="Case not found")
        
        events: List[TimelineEvent] = []
        
        # Add case creation event
        events.append(TimelineEvent(
            id=f"case-created-{case_id}",
            content=f"Case created: {case.get('title', 'Untitled')}",
            start=normalize_timestamp(case['created_at']),
            type="status",
            severity=case.get('priority'),
            metadata={"case_id": case_id, "action": "created"}
        ))
        
        # Add timeline events from case
        for idx, timeline_event in enumerate(case.get('timeline', [])):
            events.append(TimelineEvent(
                id=f"timeline-{case_id}-{idx}",
                content=timeline_event.get('event', 'Event'),
                start=normalize_timestamp(timeline_event['timestamp']),
                type="activity",
                metadata={"case_id": case_id, **timeline_event}
            ))
        
        # Add activities
        for idx, activity in enumerate(case.get('activities', [])):
            events.append(TimelineEvent(
                id=f"activity-{case_id}-{idx}",
                content=activity.get('description', 'Activity'),
                start=normalize_timestamp(activity.get('timestamp', case['created_at'])),
                type="activity",
                metadata={"case_id": case_id, **activity}
            ))
        
        # Add notes as events
        for idx, note in enumerate(case.get('notes', [])):
            events.append(TimelineEvent(
                id=f"note-{case_id}-{idx}",
                content=f"Note: {note.get('content', '')[:100]}",
                start=normalize_timestamp(note.get('timestamp', case['created_at'])),
                type="note",
                metadata={"case_id": case_id, "author": note.get('author'), "full_content": note.get('content')}
            ))
        
        # Add findings as events
        findings = data_service.get_findings_by_case(case_id)
        for finding in findings:
            events.append(TimelineEvent(
                id=f"finding-{finding['finding_id']}",
                content=f"Finding: {finding['finding_id']} - {finding.get('severity', 'unknown')}",
                start=normalize_timestamp(finding['timestamp']),
                type="finding",
                severity=finding.get('severity'),
                metadata={
                    "finding_id": finding['finding_id'],
                    "data_source": finding.get('data_source'),
                    "anomaly_score": finding.get('anomaly_score'),
                    "entity_context": finding.get('entity_context')
                }
            ))
        
        # Sort events by timestamp
        events.sort(key=lambda e: e.start)
        
        # Calculate time range
        start_time = min(e.start for e in events) if events else None
        end_time = max(e.start for e in events) if events else None
        
        return TimelineResponse(
            events=events,
            total=len(events),
            start_time=start_time,
            end_time=end_time
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting case timeline: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/finding/{finding_id}/context", response_model=TimelineResponse)
async def get_finding_context_timeline(
    finding_id: str,
    time_window_minutes: int = Query(default=60, ge=1, le=1440)
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
    try:
        data_service = DatabaseDataService()
        finding = data_service.get_finding(finding_id)
        
        if not finding:
            raise HTTPException(status_code=404, detail="Finding not found")
        
        finding_time = normalize_timestamp(finding['timestamp'])
        start_time = finding_time - timedelta(minutes=time_window_minutes)
        end_time = finding_time + timedelta(minutes=time_window_minutes)
        
        # Get findings in time window
        all_findings = data_service.get_findings(limit=1000)
        
        events: List[TimelineEvent] = []
        
        for f in all_findings:
            f_time = normalize_timestamp(f['timestamp'])
            if start_time <= f_time <= end_time:
                is_target = f['finding_id'] == finding_id
                events.append(TimelineEvent(
                    id=f"finding-{f['finding_id']}",
                    content=f"{'🎯 ' if is_target else ''}Finding: {f['finding_id']} - {f.get('severity', 'unknown')}",
                    start=f_time,
                    type="finding",
                    severity=f.get('severity'),
                    metadata={
                        "finding_id": f['finding_id'],
                        "data_source": f.get('data_source'),
                        "anomaly_score": f.get('anomaly_score'),
                        "entity_context": f.get('entity_context'),
                        "is_target": is_target
                    }
                ))
        
        # Sort events by timestamp
        events.sort(key=lambda e: e.start)
        
        return TimelineResponse(
            events=events,
            total=len(events),
            start_time=start_time,
            end_time=end_time
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting finding context timeline: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/range", response_model=TimelineResponse)
async def get_timeline_range(
    start: Optional[str] = Query(None, description="Start time (ISO format)"),
    end: Optional[str] = Query(None, description="End time (ISO format)"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    data_source: Optional[str] = Query(None, description="Filter by data source"),
    limit: int = Query(default=500, ge=1, le=5000)
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
    try:
        data_service = DatabaseDataService()
        
        # Parse time range
        start_time = normalize_timestamp(start) if start else None
        end_time = normalize_timestamp(end) if end else None
        
        # Get findings
        all_findings = data_service.get_findings(limit=limit)
        
        events: List[TimelineEvent] = []
        
        for finding in all_findings:
            f_time = normalize_timestamp(finding['timestamp'])
            
            # Filter by time range if specified
            if start_time and f_time < start_time:
                continue
            if end_time and f_time > end_time:
                continue
            
            # Filter by severity if specified
            if severity and finding.get('severity') != severity:
                continue
            
            # Filter by data source if specified
            if data_source and finding.get('data_source') != data_source:
                continue
            
            events.append(TimelineEvent(
                id=f"finding-{finding['finding_id']}",
                content=f"Finding: {finding['finding_id']} - {finding.get('severity', 'unknown')}",
                start=f_time,
                type="finding",
                severity=finding.get('severity'),
                metadata={
                    "finding_id": finding['finding_id'],
                    "data_source": finding.get('data_source'),
                    "anomaly_score": finding.get('anomaly_score'),
                    "entity_context": finding.get('entity_context')
                }
            ))
        
        # Sort events by timestamp
        events.sort(key=lambda e: e.start)
        
        # Calculate actual time range from events
        actual_start = min(e.start for e in events) if events else start_time
        actual_end = max(e.start for e in events) if events else end_time
        
        return TimelineResponse(
            events=events,
            total=len(events),
            start_time=actual_start,
            end_time=actual_end
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting timeline range: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cluster/{cluster_id}", response_model=TimelineResponse)
async def get_cluster_timeline(cluster_id: str):
    """
    Get timeline events for findings in a specific cluster.
    
    Args:
        cluster_id: Cluster identifier
        
    Returns:
        Timeline events for the cluster
    """
    try:
        data_service = DatabaseDataService()
        
        # Get findings in cluster
        all_findings = data_service.get_findings(limit=1000)
        
        # Filter by cluster_id
        findings = [f for f in all_findings if f.get('cluster_id') == cluster_id]
        
        if not findings:
            raise HTTPException(status_code=404, detail="Cluster not found or has no findings")
        
        events: List[TimelineEvent] = []
        
        for finding in findings:
            events.append(TimelineEvent(
                id=f"finding-{finding['finding_id']}",
                content=f"Finding: {finding['finding_id']} - {finding.get('severity', 'unknown')}",
                start=normalize_timestamp(finding['timestamp']),
                type="finding",
                severity=finding.get('severity'),
                metadata={
                    "finding_id": finding['finding_id'],
                    "data_source": finding.get('data_source'),
                    "anomaly_score": finding.get('anomaly_score'),
                    "entity_context": finding.get('entity_context'),
                    "cluster_id": cluster_id
                }
            ))
        
        # Sort events by timestamp
        events.sort(key=lambda e: e.start)
        
        # Calculate time range
        start_time = min(e.start for e in events) if events else None
        end_time = max(e.start for e in events) if events else None
        
        return TimelineResponse(
            events=events,
            total=len(events),
            start_time=start_time,
            end_time=end_time
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting cluster timeline: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class EventVisualizationResponse(BaseModel):
    """Event visualization data model."""
    event: TimelineEvent
    finding: Optional[Dict[str, Any]] = None
    related_events: List[TimelineEvent] = []
    entity_graph: Dict[str, Any] = {}
    mitre_techniques: List[Dict[str, Any]] = []
    ai_analysis: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = {}


@router.get("/event/{event_id}/visualization")
async def get_event_visualization(
    event_id: str,
    time_window_minutes: int = Query(default=30, ge=5, le=240),
    include_ai_analysis: bool = Query(default=True)
):
    """
    Get comprehensive visualization data for a timeline event.
    
    This endpoint provides all data needed for incident visualization:
    - Event details and metadata
    - Associated finding (if applicable)
    - Related events in time window
    - Entity relationship graph
    - MITRE ATT&CK techniques
    - AI-generated incident analysis
    
    Args:
        event_id: Event identifier (format: {type}-{id})
        time_window_minutes: Minutes before/after to include related events
        include_ai_analysis: Whether to generate AI analysis (requires Claude API)
        
    Returns:
        Comprehensive event visualization data
    """
    try:
        data_service = DatabaseDataService()
        
        # Parse event ID to determine type and actual ID
        # Format: finding-{finding_id}, activity-{case_id}-{idx}, note-{case_id}-{idx}
        event_parts = event_id.split('-', 1)
        if len(event_parts) < 2:
            raise HTTPException(status_code=400, detail="Invalid event ID format")
        
        event_type = event_parts[0]
        actual_id = event_parts[1]
        
        # Get the main event data
        event_data = None
        finding_data = None
        case_data = None
        
        if event_type == "finding":
            # This is a finding event
            finding_data = data_service.get_finding(actual_id)
            if not finding_data:
                raise HTTPException(status_code=404, detail="Finding not found")
            
            event_data = TimelineEvent(
                id=event_id,
                content=f"Finding: {finding_data['finding_id']} - {finding_data.get('severity', 'unknown')}",
                start=normalize_timestamp(finding_data['timestamp']),
                type="finding",
                severity=finding_data.get('severity'),
                metadata={
                    "finding_id": finding_data['finding_id'],
                    "data_source": finding_data.get('data_source'),
                    "anomaly_score": finding_data.get('anomaly_score'),
                    "entity_context": finding_data.get('entity_context'),
                    "description": finding_data.get('description')
                }
            )
        elif event_type in ["activity", "note", "timeline"]:
            # This is a case-related event
            case_id_parts = actual_id.split('-')
            if len(case_id_parts) >= 2:
                case_id = f"{case_id_parts[0]}-{case_id_parts[1]}"
                case_data = data_service.get_case(case_id)
                if not case_data:
                    raise HTTPException(status_code=404, detail="Case not found")
                
                # Try to find the specific event in the case data
                # This is a simplified version - in production you'd want to store events with IDs
                event_data = TimelineEvent(
                    id=event_id,
                    content=f"{event_type.capitalize()} event",
                    start=normalize_timestamp(case_data['created_at']),
                    type=event_type,
                    metadata={"case_id": case_id}
                )
        else:
            raise HTTPException(status_code=400, detail="Unknown event type")
        
        if not event_data:
            raise HTTPException(status_code=404, detail="Event not found")
        
        # Get related events in time window
        event_time = event_data.start
        start_time = event_time - timedelta(minutes=time_window_minutes)
        end_time = event_time + timedelta(minutes=time_window_minutes)
        
        related_events: List[TimelineEvent] = []
        all_findings = data_service.get_findings(limit=1000)
        
        for f in all_findings:
            f_time = normalize_timestamp(f['timestamp'])
            if start_time <= f_time <= end_time and f['finding_id'] != finding_data.get('finding_id') if finding_data else True:
                related_events.append(TimelineEvent(
                    id=f"finding-{f['finding_id']}",
                    content=f"Finding: {f['finding_id']} - {f.get('severity', 'unknown')}",
                    start=f_time,
                    type="finding",
                    severity=f.get('severity'),
                    metadata={
                        "finding_id": f['finding_id'],
                        "data_source": f.get('data_source'),
                        "anomaly_score": f.get('anomaly_score'),
                        "entity_context": f.get('entity_context')
                    }
                ))
        
        # Sort related events by time
        related_events.sort(key=lambda e: e.start)
        
        # Build entity graph for this event and related events
        from core.findings.graph_builder_service import GraphBuilderService
        graph_builder = GraphBuilderService()
        
        findings_for_graph = []
        if finding_data:
            findings_for_graph.append(finding_data)
        # Add related findings
        for re in related_events[:10]:  # Limit to 10 related events for graph
            if re.metadata and re.metadata.get('finding_id'):
                rf = data_service.get_finding(re.metadata['finding_id'])
                if rf:
                    findings_for_graph.append(rf)
        
        entity_graph = {}
        if findings_for_graph:
            entity_graph = graph_builder.build_entity_graph(findings_for_graph)
        
        # Extract MITRE ATT&CK techniques
        mitre_techniques = []
        if finding_data:
            mitre_preds = finding_data.get('mitre_predictions', {})
            predicted_techniques = finding_data.get('predicted_techniques', [])
            
            if mitre_preds:
                for technique_id, confidence in sorted(mitre_preds.items(), key=lambda x: x[1], reverse=True)[:5]:
                    mitre_techniques.append({
                        "technique_id": technique_id,
                        "confidence": confidence,
                        "name": technique_id  # In production, look up technique name
                    })
            elif predicted_techniques:
                for tech in predicted_techniques[:5]:
                    mitre_techniques.append({
                        "technique_id": tech,
                        "confidence": 0.0,
                        "name": tech
                    })
        
        # Generate AI analysis if requested
        ai_analysis = None
        if include_ai_analysis and finding_data:
            try:
                from core.llm.harness.claude import ClaudeService
                claude_service = ClaudeService(use_backend_tools=True, use_mcp_tools=False)
                
                if claude_service.has_api_key():
                    ai_analysis = await claude_service.generate_event_analysis(
                        event_data.model_dump(),
                        [e.model_dump() for e in related_events],
                        finding_data
                    )
            except Exception as e:
                logger.warning(f"Failed to generate AI analysis: {e}")
                ai_analysis = {"error": "AI analysis unavailable"}
        
        # Build response
        return EventVisualizationResponse(
            event=event_data,
            finding=finding_data,
            related_events=related_events,
            entity_graph=entity_graph,
            mitre_techniques=mitre_techniques,
            ai_analysis=ai_analysis,
            metadata={
                "time_window_minutes": time_window_minutes,
                "related_events_count": len(related_events),
                "entities_count": len(entity_graph.get('nodes', [])),
                "mitre_techniques_count": len(mitre_techniques)
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting event visualization: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

