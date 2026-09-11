import json
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Iterator, Optional

from mcp.server.mcpserver import MCPServer

from core.agents.projections import pack_completed_hunts
from core.time import utcnow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
mcp = MCPServer("deeptempo-findings")

_data_service = None


class _JsonEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat() + "Z"
        return super().default(obj)


def jdump(obj, indent=2):
    return json.dumps(obj, cls=_JsonEncoder, indent=indent)


def get_data_service():
    """Return the shared DatabaseDataService (demo mode or PostgreSQL)."""
    global _data_service
    if _data_service is None:
        from core.storage.database_data_service import DatabaseDataService

        _data_service = DatabaseDataService()
        backend_info = _data_service.get_backend_info()
        logger.info(f"MCP deeptempo-findings using backend: {backend_info['backend']}")

    return _data_service


def load_findings():
    """Load findings from DatabaseDataService."""
    try:
        return get_data_service().get_findings(limit=10000)
    except Exception as e:
        logger.error(f"Error loading findings via DatabaseDataService: {e}")
        return []


def get_db():
    """Get DatabaseService for direct database operations."""
    from core.storage.service import DatabaseService

    return DatabaseService()


@mcp.tool()
def list_findings(
    severity: Optional[str] = None,
    data_source: Optional[str] = None,
    cluster_id: Optional[str] = None,
    min_anomaly_score: Optional[float] = None,
    limit: int = 50,
    **kwargs,
) -> str:
    try:
        findings = load_findings()
        if severity:
            findings = [f for f in findings if f.get("severity") == severity]
        if data_source:
            findings = [f for f in findings if f.get("data_source") == data_source]
        if cluster_id:
            findings = [f for f in findings if f.get("cluster_id") == cluster_id]
        if min_anomaly_score is not None:
            findings = [
                f for f in findings if f.get("anomaly_score", 0) >= min_anomaly_score
            ]

        results = findings[:limit]
        return jdump(
            {"total": len(findings), "returned": len(results), "findings": results}
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def get_finding(finding_id: str, **kwargs) -> str:
    """
    Get a specific finding by ID.

    Uses DatabaseDataService for efficient single-record lookup.
    """
    try:
        data_service = get_data_service()
        finding = data_service.get_finding(finding_id)

        if finding:
            return jdump(finding)

        return jdump({"error": f"Finding {finding_id} not found"})
    except Exception as e:
        logger.error(f"Error getting finding {finding_id}: {e}")
        return jdump({"error": str(e)})


@mcp.tool()
async def list_completed_hunts(
    start: str,
    end: str,
    limit: int = 200,
    **kwargs,
) -> str:
    """Return completed threat-hunt projections for an assessment window."""
    try:
        return jdump(await pack_completed_hunts(start=start, end=end, limit=limit))
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def technique_rollup(min_confidence: float = 0.5, **kwargs) -> str:
    try:
        findings = load_findings()
        stats = {}
        for f in findings:
            for tech, conf in f.get("mitre_predictions", {}).items():
                if conf >= min_confidence:
                    if tech not in stats:
                        stats[tech] = {"count": 0, "total": 0}
                    stats[tech]["count"] += 1
                    stats[tech]["total"] += conf

        results = [
            {
                "technique": t,
                "count": int(s["count"]),
                "avg_confidence": round(s["total"] / s["count"], 3),
            }
            for t, s in stats.items()
        ]
        results.sort(key=lambda x: x["count"], reverse=True)
        return jdump({"min_confidence": min_confidence, "techniques": results})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def list_cases(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 50,
    **kwargs,
) -> str:
    try:
        db = get_db()
        cases = db.get_cases(status=status, priority=priority, limit=limit)
        results = [
            {
                "case_id": c.case_id,
                "title": c.title,
                "status": c.status,
                "priority": c.priority,
                "assignee": c.assignee,
                "finding_count": len(c.findings) if hasattr(c, "findings") else 0,
                "created_at": c.created_at,
                "updated_at": c.updated_at,
            }
            for c in cases
        ]
        return jdump({"total": len(results), "cases": results})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def get_case(case_id: str, **kwargs) -> str:
    try:
        db = get_db()
        case = db.get_case(case_id, include_findings=True)
        if not case:
            return jdump({"error": f"Case {case_id} not found"})

        result = {
            "case_id": case.case_id,
            "title": case.title,
            "description": case.description,
            "status": case.status,
            "priority": case.priority,
            "assignee": case.assignee,
            "tags": case.tags or [],
            "notes": case.notes or [],
            "timeline": case.timeline or [],
            "activities": case.activities or [],
            "resolution_steps": case.resolution_steps or [],
            "mitre_techniques": case.mitre_techniques or [],
            "created_at": case.created_at,
            "updated_at": case.updated_at,
        }
        if hasattr(case, "findings"):
            result["findings"] = [
                {
                    "finding_id": f.finding_id,
                    "severity": f.severity,
                    "data_source": f.data_source,
                    "anomaly_score": float(f.anomaly_score or 0),
                    "timestamp": f.timestamp,
                    "status": f.status,
                }
                for f in case.findings
            ]
        return jdump(result)
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def create_case(
    title: str,
    finding_ids: list,
    description: str = "",
    priority: str = "medium",
    status: str = "new",
    assignee: Optional[str] = None,
    tags: Optional[list] = None,
    **kwargs,
) -> str:
    try:
        db = get_db()
        case_id = f"case-{utcnow().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
        case = db.create_case(
            case_id=case_id,
            title=title,
            finding_ids=finding_ids,
            description=description,
            status=status,
            priority=priority,
            assignee=assignee,
            tags=tags or [],
        )
        if not case:
            return jdump({"error": "Failed to create case"})
        return jdump(
            {
                "success": True,
                "case_id": case.case_id,
                "title": case.title,
                "status": case.status,
                "finding_count": len(finding_ids),
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@contextmanager
def _service_session() -> Iterator["Session"]:
    """A session of this tool's own, committed if the work returns.

    This tool reaches the database directly rather than through the API, so
    every call into a service here has to own its transaction. One definition of
    that, because a second would be a second answer to when the work commits.
    """
    from core.storage.connection import get_db_session

    session = get_db_session()
    try:
        yield session
        session.commit()
    finally:
        session.close()


def _close_through_the_service(case_id: str, **kwargs) -> None:
    """Close a Case the one way a Case is closed.

    Going through the service is what makes an agent's close the same shape as
    everyone else's -- the SLA resolution clock stops and the Case's IOCs are
    indexed, neither of which happens when a caller writes the closure row
    itself.
    """
    from core.cases.case_workflow_service import CaseWorkflowService

    with _service_session() as session:
        CaseWorkflowService().close_case(session, case_id, **kwargs)


def _record_agent_close(case_id: str) -> None:
    """Record that an agent closed this Case, and stated no category.

    `unspecified` is not a determination and does not pretend to be one: the
    Case closed and no reason was given, which is what happened, and becomes an
    inconclusive Verdict rather than a claim nobody made. An agent that has a
    determination calls `close_case` and says which. The service refuses to let
    this overwrite a determination already on record.

    Trust is `agent` unconditionally here. There is no authenticated person
    behind an MCP call, and `analyst` is the one record this system will not let
    an agent claim on its own behalf.
    """
    from core.cases.closure import ClosedByKind, ClosureCategory

    _close_through_the_service(
        case_id,
        closure_category=ClosureCategory.UNSPECIFIED,
        closed_by="agent",
        closed_by_kind=ClosedByKind.AGENT,
    )


def _record_reopen(case_id: str) -> None:
    """Retract what closing the Case determined, keeping what it wrote.

    The write-up survives -- root cause and lessons learned are work, not a
    verdict. The category does not: left standing, the next status edit states
    no category of its own and would close the Case back into the determination
    the reopen retracted.
    """
    from core.cases.case_workflow_service import CaseWorkflowService

    with _service_session() as session:
        CaseWorkflowService().reopen_case(session, case_id)


@mcp.tool()
def update_case(
    case_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    assignee: Optional[str] = None,
    add_note: Optional[str] = None,
    **kwargs,
) -> str:
    try:
        db = get_db()
        case = db.get_case(case_id)
        if not case:
            return jdump({"error": f"Case {case_id} not found"})

        updates = {}
        if title:
            updates["title"] = title
        if description:
            updates["description"] = description
        if status:
            updates["status"] = status
        if priority:
            updates["priority"] = priority
        if assignee:
            updates["assignee"] = assignee
        if add_note:
            notes = case.notes or []
            notes.append(
                {"timestamp": utcnow().isoformat() + "Z", "note": add_note}
            )
            updates["notes"] = notes

        was_closed = (case.status or "").strip() == "closed"
        if not db.update_case(case_id, **updates):
            return jdump({"error": "Failed to update case"})

        # The status edit is a close, so it records one -- the same fact the
        # console's PATCH records, from the other side. An agent closing this
        # way used to leave no closure row at all, so episodic memory read the
        # close off `cases.updated_at`, which moves on every later edit and
        # re-derives the Verdict for changes that concluded nothing.
        if updates.get("status") == "closed" and not was_closed:
            _record_agent_close(case_id)
        elif was_closed and updates.get("status") not in (None, "closed"):
            _record_reopen(case_id)

        return jdump({"success": True, "case_id": case_id})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_finding_to_case(case_id: str, finding_id: str, **kwargs) -> str:
    try:
        db = get_db()
        if db.add_finding_to_case(case_id, finding_id):
            return jdump(
                {"success": True, "message": f"Added {finding_id} to {case_id}"}
            )
        return jdump({"error": "Failed to add finding"})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def remove_finding_from_case(case_id: str, finding_id: str, **kwargs) -> str:
    try:
        db = get_db()
        if db.remove_finding_from_case(case_id, finding_id):
            return jdump(
                {"success": True, "message": f"Removed {finding_id} from {case_id}"}
            )
        return jdump({"error": "Failed to remove finding"})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_activity(
    case_id: str,
    activity_type: str,
    description: str,
    details: Optional[dict] = None,
    **kwargs,
) -> str:
    """
    Add an activity/action to a case. Activities track actions taken during investigation.

    Args:
        case_id: The case ID (e.g., "case-20260114-abc123")
        activity_type: Type of activity (e.g., "note", "status_change", "finding_added",
                      "action_taken", "investigation_step", "analysis", "communication")
        description: Description of the activity
        details: Optional dictionary with additional details

    Examples:
        - add_case_activity("case-123", "note", "Confirmed lateral movement pattern")
        - add_case_activity("case-123", "action_taken", "Isolated infected host",
                          {"host": "workstation-42", "action": "network_isolation"})
    """
    try:
        db = get_db()
        case = db.get_case(case_id)
        if not case:
            return jdump({"error": f"Case {case_id} not found"})

        activities = case.activities or []
        new_activity = {
            "timestamp": utcnow().isoformat() + "Z",
            "activity_type": activity_type,
            "description": description,
            "details": details or {},
        }
        activities.append(new_activity)

        if db.update_case(case_id, activities=activities):
            return jdump(
                {
                    "success": True,
                    "message": f"Added {activity_type} activity to {case_id}",
                    "activity": new_activity,
                }
            )
        return jdump({"error": "Failed to add activity"})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_timeline_entry(
    case_id: str,
    event_description: str,
    event_time: Optional[str] = None,
    event_type: str = "investigation",
    details: Optional[dict] = None,
    **kwargs,
) -> str:
    """
    Add an entry to the case timeline. Timeline tracks chronological events.

    Args:
        case_id: The case ID
        event_description: Description of the event
        event_time: ISO timestamp of when the event occurred (defaults to now)
        event_type: Type of event (e.g., "attack", "detection", "investigation", "response")
        details: Optional additional details

    Examples:
        - add_case_timeline_entry("case-123", "Initial malware execution detected",
                                "2026-01-21T10:00:00Z", "attack")
        - add_case_timeline_entry("case-123", "Analyst began investigation", event_type="investigation")
    """
    try:
        db = get_db()
        case = db.get_case(case_id)
        if not case:
            return jdump({"error": f"Case {case_id} not found"})

        timeline = case.timeline or []
        new_entry = {
            "timestamp": event_time or (utcnow().isoformat() + "Z"),
            "event_type": event_type,
            "description": event_description,
            "details": details or {},
        }
        timeline.append(new_entry)

        # Sort timeline by timestamp
        timeline.sort(key=lambda x: x["timestamp"])

        if db.update_case(case_id, timeline=timeline):
            return jdump(
                {
                    "success": True,
                    "message": f"Added timeline entry to {case_id}",
                    "entry": new_entry,
                }
            )
        return jdump({"error": "Failed to add timeline entry"})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_mitre_techniques(case_id: str, technique_ids: list, **kwargs) -> str:
    """
    Add MITRE ATT&CK technique IDs to a case to document the kill chain.

    Args:
        case_id: The case ID
        technique_ids: List of MITRE technique IDs (e.g., ["T1071.001", "T1059.001"])

    Example:
        add_case_mitre_techniques("case-123", ["T1071.001", "T1059.001", "T1048.003"])
    """
    try:
        db = get_db()
        case = db.get_case(case_id)
        if not case:
            return jdump({"error": f"Case {case_id} not found"})

        existing_techniques = set(case.mitre_techniques or [])
        new_techniques = set(technique_ids)
        combined_techniques = list(existing_techniques.union(new_techniques))

        if db.update_case(case_id, mitre_techniques=combined_techniques):
            added = list(new_techniques - existing_techniques)
            return jdump(
                {
                    "success": True,
                    "message": f"Added {len(added)} new techniques to {case_id}",
                    "added_techniques": added,
                    "all_techniques": combined_techniques,
                }
            )
        return jdump({"error": "Failed to add techniques"})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_resolution_step(
    case_id: str,
    description: str,
    action_taken: str,
    result: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Add a resolution/remediation step to a case.

    Args:
        case_id: The case ID
        description: Description of what was done
        action_taken: The specific action taken
        result: Result or outcome of the action

    Example:
        add_resolution_step("case-123", "Containment",
                          "Isolated infected hosts from network",
                          "3 workstations successfully isolated")
    """
    try:
        db = get_db()
        case = db.get_case(case_id)
        if not case:
            return jdump({"error": f"Case {case_id} not found"})

        resolution_steps = case.resolution_steps or []
        new_step = {
            "timestamp": utcnow().isoformat() + "Z",
            "description": description,
            "action_taken": action_taken,
            "result": result,
        }
        resolution_steps.append(new_step)

        if db.update_case(case_id, resolution_steps=resolution_steps):
            return jdump(
                {
                    "success": True,
                    "message": f"Added resolution step to {case_id}",
                    "step": new_step,
                }
            )
        return jdump({"error": "Failed to add resolution step"})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def bulk_add_findings_to_case(
    case_id: str, finding_ids: list, note: Optional[str] = None, **kwargs
) -> str:
    """
    Add multiple findings to a case at once.

    Args:
        case_id: The case ID
        finding_ids: List of finding IDs to add
        note: Optional note explaining why these findings were added

    Example:
        bulk_add_findings_to_case("case-123",
                                ["f-20260121-001", "f-20260121-002", "f-20260121-003"],
                                "All findings show lateral movement pattern")
    """
    try:
        db = get_db()
        case = db.get_case(case_id)
        if not case:
            return jdump({"error": f"Case {case_id} not found"})

        added = []
        failed = []

        for finding_id in finding_ids:
            try:
                if db.add_finding_to_case(case_id, finding_id):
                    added.append(finding_id)
                else:
                    failed.append(finding_id)
            except Exception:
                failed.append(finding_id)

        # Add activity noting what was added
        if added and note:
            add_case_activity(
                case_id,
                "finding_added",
                f"Added {len(added)} findings: {note}",
                {"finding_ids": added, "note": note},
            )

        return jdump(
            {
                "success": len(added) > 0,
                "added_count": len(added),
                "failed_count": len(failed),
                "added_findings": added,
                "failed_findings": failed,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def create_case_from_killchain(
    title: str,
    finding_ids: list,
    killchain_stages: list,
    description: str = "",
    priority: str = "high",
    assignee: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Create a case documenting a kill chain with findings organized by stage.

    Args:
        title: Case title
        finding_ids: List of finding IDs
        killchain_stages: List of dicts with 'stage' and 'techniques' keys
                         Example: [{"stage": "Initial Access", "techniques": ["T1078"]},
                                  {"stage": "Lateral Movement", "techniques": ["T1021.001"]}]
        description: Case description
        priority: Priority level
        assignee: Optional assignee

    Example:
        create_case_from_killchain(
            "APT Lateral Movement Campaign",
            ["f-001", "f-002", "f-003"],
            [
                {"stage": "Initial Access", "techniques": ["T1078"], "description": "Compromised credentials"},
                {"stage": "Lateral Movement", "techniques": ["T1021.001"], "description": "RDP lateral movement"},
                {"stage": "Exfiltration", "techniques": ["T1048.003"], "description": "Data staged for exfil"}
            ],
            priority="critical"
        )
    """
    try:
        # Create the case
        result = create_case(
            title=title,
            finding_ids=finding_ids,
            description=description,
            priority=priority,
            status="open",
            assignee=assignee,
            tags=["killchain", "apt"]
            + [
                stage.get("stage", "").lower().replace(" ", "_")
                for stage in killchain_stages
            ],
        )

        result_dict = json.loads(result)
        if not result_dict.get("success"):
            return result

        case_id = result_dict["case_id"]

        # Add timeline entries for each stage
        for i, stage in enumerate(killchain_stages):
            stage_name = stage.get("stage", f"Stage {i+1}")
            stage_desc = stage.get("description", "")
            techniques = stage.get("techniques", [])

            add_case_timeline_entry(
                case_id,
                f"{stage_name}: {stage_desc}",
                event_type="attack",
                details={"stage": stage_name, "techniques": techniques},
            )

            # Add MITRE techniques if provided
            if techniques:
                add_case_mitre_techniques(case_id, techniques)

        # Add initial activity
        add_case_activity(
            case_id,
            "investigation_step",
            f"Case created for kill chain analysis with {len(killchain_stages)} stages",
            {"stages": [s.get("stage") for s in killchain_stages]},
        )

        return jdump(
            {
                "success": True,
                "case_id": case_id,
                "title": title,
                "stages": len(killchain_stages),
                "finding_count": len(finding_ids),
                "message": f"Created case with {len(killchain_stages)} kill chain stages",
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_comment(
    case_id: str,
    author: str,
    content: str,
    parent_comment_id: Optional[int] = None,
    **kwargs,
) -> str:
    """
    Add a comment to a case. Supports threaded discussions.

    Args:
        case_id: The case ID
        author: Username of the comment author
        content: Comment text
        parent_comment_id: Optional ID of parent comment for threading

    Examples:
        - add_case_comment("case-123", "analyst1", "Confirmed lateral movement pattern")
        - add_case_comment("case-123", "analyst2", "I see the same pattern", parent_comment_id=5)
    """
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import CaseComment

        session = get_db_session()
        try:
            comment = CaseComment(
                case_id=case_id,
                author=author,
                content=content,
                parent_comment_id=parent_comment_id,
                is_edited=False,
                is_deleted=False,
            )
            session.add(comment)
            session.commit()

            return jdump(
                {
                    "success": True,
                    "comment_id": comment.comment_id,
                    "message": f"Added comment to {case_id}",
                    "comment": comment.to_dict(),
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def get_case_comments(case_id: str, **kwargs) -> str:
    """Get all comments for a case."""
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import CaseComment

        session = get_db_session()
        try:
            comments = (
                session.query(CaseComment)
                .filter(CaseComment.case_id == case_id, CaseComment.is_deleted == False)
                .order_by(CaseComment.created_at)
                .all()
            )

            return jdump(
                {
                    "case_id": case_id,
                    "comment_count": len(comments),
                    "comments": [c.to_dict() for c in comments],
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_evidence(
    case_id: str,
    evidence_type: str,
    name: str,
    collected_by: str,
    description: Optional[str] = None,
    file_path: Optional[str] = None,
    source: Optional[str] = None,
    tags: Optional[list] = None,
    **kwargs,
) -> str:
    """
    Add evidence to a case with chain of custody tracking.

    Args:
        case_id: The case ID
        evidence_type: Type (e.g., "file", "log", "network_capture", "memory_dump", "screenshot")
        name: Evidence name
        collected_by: Who collected it
        description: Optional description
        file_path: Optional file path
        source: Optional source system
        tags: Optional tags list

    Examples:
        - add_case_evidence("case-123", "memory_dump", "host-42-memory.raw", "analyst1",
                           description="Memory dump from compromised host")
        - add_case_evidence("case-123", "log", "firewall-logs.txt", "soc-team",
                           source="Palo Alto FW", tags=["c2", "exfiltration"])
    """
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import CaseEvidence

        session = get_db_session()
        try:
            evidence = CaseEvidence(
                case_id=case_id,
                evidence_type=evidence_type,
                name=name,
                collected_by=collected_by,
                collected_at=utcnow(),
                description=description,
                file_path=file_path,
                source=source,
                tags=tags or [],
                chain_of_custody=[
                    {
                        "timestamp": utcnow().isoformat() + "Z",
                        "action": "collected",
                        "user": collected_by,
                        "notes": "Evidence collected and added to case",
                    }
                ],
            )
            session.add(evidence)
            session.commit()

            return jdump(
                {
                    "success": True,
                    "evidence_id": evidence.evidence_id,
                    "message": f"Added evidence '{name}' to {case_id}",
                    "evidence": evidence.to_dict(),
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_ioc(
    case_id: str,
    ioc_type: str,
    value: str,
    threat_level: Optional[str] = None,
    confidence: Optional[float] = None,
    source: Optional[str] = None,
    tags: Optional[list] = None,
    context: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Add an Indicator of Compromise (IOC) to a case.

    Args:
        case_id: The case ID
        ioc_type: Type (e.g., "ip", "domain", "hash", "url", "email", "file_name")
        value: The IOC value
        threat_level: Optional threat level ("critical", "high", "medium", "low")
        confidence: Optional confidence score (0.0-1.0)
        source: Optional source of the IOC
        tags: Optional tags
        context: Optional context/notes

    Examples:
        - add_case_ioc("case-123", "ip", "192.168.50.5", threat_level="high",
                      context="C2 server IP")
        - add_case_ioc("case-123", "domain", "evil.com", threat_level="critical",
                      confidence=0.95, source="VirusTotal")
        - add_case_ioc("case-123", "hash", "a1b2c3...", ioc_type="md5",
                      tags=["malware", "ransomware"])
    """
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import CaseIOC

        session = get_db_session()
        try:
            ioc = CaseIOC(
                case_id=case_id,
                ioc_type=ioc_type,
                value=value,
                threat_level=threat_level,
                confidence=confidence,
                source=source,
                tags=tags or [],
                context=context,
                first_seen=utcnow(),
                last_seen=utcnow(),
                is_active=True,
                is_false_positive=False,
            )
            session.add(ioc)
            session.commit()

            return jdump(
                {
                    "success": True,
                    "ioc_id": ioc.ioc_id,
                    "message": f"Added IOC {ioc_type}:{value} to {case_id}",
                    "ioc": ioc.to_dict(),
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def bulk_add_iocs(case_id: str, iocs: list, **kwargs) -> str:
    """
    Bulk add multiple IOCs to a case at once.

    Args:
        case_id: The case ID
        iocs: List of IOC dicts with keys: ioc_type, value, threat_level (optional),
              confidence (optional), source (optional), context (optional)

    Example:
        bulk_add_iocs("case-123", [
            {"ioc_type": "ip", "value": "192.168.1.5", "threat_level": "high"},
            {"ioc_type": "ip", "value": "192.168.1.6", "threat_level": "high"},
            {"ioc_type": "domain", "value": "evil.com", "threat_level": "critical"}
        ])
    """
    try:
        added = 0
        failed = 0
        results = []

        for ioc_data in iocs:
            try:
                result = add_case_ioc(
                    case_id=case_id,
                    ioc_type=ioc_data.get("ioc_type"),
                    value=ioc_data.get("value"),
                    threat_level=ioc_data.get("threat_level"),
                    confidence=ioc_data.get("confidence"),
                    source=ioc_data.get("source"),
                    context=ioc_data.get("context"),
                    tags=ioc_data.get("tags"),
                )
                result_dict = json.loads(result)
                if result_dict.get("success"):
                    added += 1
                else:
                    failed += 1
                results.append(result_dict)
            except Exception as e:
                failed += 1
                results.append({"error": str(e), "ioc": ioc_data})

        return jdump(
            {
                "success": added > 0,
                "added": added,
                "failed": failed,
                "total": len(iocs),
                "results": results,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def get_case_iocs(case_id: str, ioc_type: Optional[str] = None, **kwargs) -> str:
    """Get all IOCs for a case, optionally filtered by type."""
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import CaseIOC

        session = get_db_session()
        try:
            query = session.query(CaseIOC).filter(CaseIOC.case_id == case_id)
            if ioc_type:
                query = query.filter(CaseIOC.ioc_type == ioc_type)

            iocs = query.all()

            return jdump(
                {
                    "case_id": case_id,
                    "ioc_count": len(iocs),
                    "iocs": [ioc.to_dict() for ioc in iocs],
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_task(
    case_id: str,
    title: str,
    description: Optional[str] = None,
    assignee: Optional[str] = None,
    priority: str = "medium",
    due_date: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Add a task to a case for tracking investigation work.

    Args:
        case_id: The case ID
        title: Task title
        description: Optional description
        assignee: Optional assignee username
        priority: Priority ("low", "medium", "high", "critical")
        due_date: Optional due date (ISO format)

    Examples:
        - add_case_task("case-123", "Analyze malware sample", priority="high")
        - add_case_task("case-123", "Interview affected users", assignee="analyst2",
                       due_date="2026-01-25T17:00:00Z")
    """
    try:
        from datetime import datetime

        from core.storage.connection import get_db_session
        from core.storage.models import CaseTask

        session = get_db_session()
        try:
            task = CaseTask(
                case_id=case_id,
                title=title,
                description=description,
                assignee=assignee,
                priority=priority,
                status="pending",
                due_date=(
                    datetime.fromisoformat(due_date.replace("Z", "+00:00"))
                    if due_date
                    else None
                ),
            )
            session.add(task)
            session.commit()

            return jdump(
                {
                    "success": True,
                    "task_id": task.task_id,
                    "message": f"Added task '{title}' to {case_id}",
                    "task": task.to_dict(),
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def update_case_task(
    task_id: int,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    notes: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Update a task status.

    Args:
        task_id: The task ID
        status: Optional new status ("pending", "in_progress", "completed", "cancelled")
        assignee: Optional new assignee
        notes: Optional notes about the update

    Examples:
        - update_case_task(5, status="in_progress")
        - update_case_task(5, status="completed", notes="Malware analysis complete - ransomware variant")
    """
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import CaseTask

        session = get_db_session()
        try:
            task = session.query(CaseTask).filter(CaseTask.task_id == task_id).first()
            if not task:
                return jdump({"error": f"Task {task_id} not found"})

            if status:
                task.status = status
                if status == "completed":
                    task.completed_at = utcnow()
            if assignee:
                task.assignee = assignee

            session.commit()

            # Also add activity to the case if status changed
            if status:
                add_case_activity(
                    task.case_id,
                    "task_update",
                    f"Task '{task.title}' status changed to {status}",
                    (
                        {"task_id": task_id, "notes": notes}
                        if notes
                        else {"task_id": task_id}
                    ),
                )

            return jdump(
                {
                    "success": True,
                    "message": f"Updated task {task_id}",
                    "task": task.to_dict(),
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def get_case_tasks(case_id: str, **kwargs) -> str:
    """Get all tasks for a case."""
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import CaseTask

        session = get_db_session()
        try:
            tasks = (
                session.query(CaseTask)
                .filter(CaseTask.case_id == case_id)
                .order_by(CaseTask.task_order, CaseTask.created_at)
                .all()
            )

            return jdump(
                {
                    "case_id": case_id,
                    "task_count": len(tasks),
                    "tasks": [t.to_dict() for t in tasks],
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def link_related_cases(
    case_id: str,
    related_case_id: str,
    relationship_type: str,
    created_by: str,
    notes: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Link two related cases together.

    Args:
        case_id: Primary case ID
        related_case_id: Related case ID
        relationship_type: Type ("duplicate", "related", "parent", "child", "blocks", "blocked_by")
        created_by: Username of person creating link
        notes: Optional notes about relationship

    Examples:
        - link_related_cases("case-123", "case-124", "related", "analyst1",
                            notes="Both cases show same attack pattern")
        - link_related_cases("case-123", "case-125", "parent", "analyst1",
                            notes="case-123 is the parent campaign")
    """
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import CaseRelationship

        session = get_db_session()
        try:
            relationship = CaseRelationship(
                case_id=case_id,
                related_case_id=related_case_id,
                relationship_type=relationship_type,
                created_by=created_by,
                notes=notes,
            )
            session.add(relationship)
            session.commit()

            # Add activity to both cases
            add_case_activity(
                case_id,
                "case_linked",
                f"Linked to {related_case_id} ({relationship_type})",
                {
                    "related_case_id": related_case_id,
                    "relationship_type": relationship_type,
                },
            )

            return jdump(
                {
                    "success": True,
                    "relationship_id": relationship.relationship_id,
                    "message": f"Linked {case_id} to {related_case_id} as {relationship_type}",
                    "relationship": relationship.to_dict(),
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def escalate_case(
    case_id: str,
    escalated_from: str,
    escalated_to: str,
    reason: str,
    urgency_level: str = "high",
    **kwargs,
) -> str:
    """
    Escalate a case to higher tier or management.

    Args:
        case_id: The case ID
        escalated_from: Who is escalating (username)
        escalated_to: Who to escalate to (username/team)
        reason: Reason for escalation
        urgency_level: Urgency ("low", "medium", "high", "critical")

    Example:
        escalate_case("case-123", "analyst1", "soc-manager",
                     "Suspected APT activity requires management approval",
                     urgency_level="critical")
    """
    try:
        from core.storage.connection import get_db_session
        from core.storage.models import CaseEscalation

        session = get_db_session()
        try:
            escalation = CaseEscalation(
                case_id=case_id,
                escalated_from=escalated_from,
                escalated_to=escalated_to,
                reason=reason,
                urgency_level=urgency_level,
                status="pending",
            )
            session.add(escalation)
            session.commit()

            # Add activity
            add_case_activity(
                case_id,
                "escalation",
                f"Case escalated to {escalated_to}: {reason}",
                {"escalation_id": escalation.escalation_id, "urgency": urgency_level},
            )

            # Update case priority if critical
            if urgency_level == "critical":
                db = get_db()
                db.update_case(case_id, priority="critical")

            return jdump(
                {
                    "success": True,
                    "escalation_id": escalation.escalation_id,
                    "message": f"Escalated {case_id} to {escalated_to}",
                    "escalation": escalation.to_dict(),
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def close_case(
    case_id: str,
    closure_category: str,
    closed_by: str,
    root_cause: Optional[str] = None,
    lessons_learned: Optional[str] = None,
    recommendations: Optional[str] = None,
    executive_summary: Optional[str] = None,
    false_positive_reason: Optional[str] = None,
    closure_notes: Optional[str] = None,
    **kwargs,
) -> str:
    """
    Properly close a case with closure metadata.

    Args:
        case_id: The case ID
        closure_category: Category ("resolved", "false_positive", "duplicate", "unable_to_resolve")
        closed_by: Who is closing the case
        root_cause: Optional root cause analysis
        lessons_learned: Optional lessons learned
        recommendations: Optional recommendations
        executive_summary: Optional executive summary
        false_positive_reason: Optional reason a false-positive closure was one
        closure_notes: Optional free-text notes on the closure

    Example:
        close_case("case-123", "resolved", "analyst1",
                  root_cause="Compromised credentials due to phishing",
                  lessons_learned="Need MFA enforcement",
                  recommendations="Deploy MFA to all users, additional phishing training",
                  executive_summary="Lateral movement attack contained and remediated")
    """
    try:
        from core.cases.case_workflow_service import CaseWorkflowService
        from core.cases.closure import ClosedByKind, ClosureCategory
        from core.storage.connection import get_db_session

        # Stated here rather than left to the mapping. An unknown category
        # closes the Case and then reaches memory as nothing -- the Distil has
        # no outcome for one, so it writes a marker and no Verdict and the Case
        # never comes back. The API rejects one; so does this.
        try:
            category = ClosureCategory(str(closure_category).strip().lower())
        except ValueError:
            return jdump(
                {
                    "error": f"{closure_category!r} is not a closure category",
                    "categories": [member.value for member in ClosureCategory],
                }
            )

        session = get_db_session()
        try:
            # Through the service rather than writing the rows here. This tool
            # had its own copy of the close, so the SLA clock, the IOC index and
            # anything added to a close later were the service's alone -- and a
            # case closed by an agent was a different shape of closed from one
            # closed through the API.
            closure = CaseWorkflowService().close_case(
                session,
                case_id,
                closure_category=category,
                closed_by=closed_by,
                # No authenticated person behind an MCP call. Episodic memory
                # reads this as Trust, and `analyst` is the one record this
                # system will not let an agent claim on its own behalf.
                closed_by_kind=ClosedByKind.AGENT,
                root_cause=root_cause,
                lessons_learned=lessons_learned,
                recommendations=recommendations,
                executive_summary=executive_summary,
                false_positive_reason=false_positive_reason,
                closure_notes=closure_notes,
            )
            if closure is None:
                return jdump({"error": f"Case {case_id} not found"})

            payload = closure.to_dict()
            session.commit()

            # Add final activity
            add_case_activity(
                case_id,
                "case_closed",
                f"Case closed as {category.value}",
                {"closure_category": category.value, "closed_by": closed_by},
            )

            return jdump(
                {
                    "success": True,
                    "message": f"Closed {case_id} as {category.value}",
                    "closure": payload,
                }
            )
        finally:
            session.close()
    except Exception as e:
        return jdump({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
