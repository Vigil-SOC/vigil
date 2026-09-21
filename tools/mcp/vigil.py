import json
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Iterator, Optional

from mcp.server.mcpserver import MCPServer

from core.agents.projections import pack_completed_hunts, read_replay
from core.time import utcnow

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
mcp = MCPServer("vigil")

_data_service = None


class _JsonEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat() + "Z"
        return super().default(obj)


# Who this server acts as when it writes a name into a record.
#
# Every tool that used to take the actor as an argument asks this instead -- a
# caller that supplies its own name is not identifying itself, it is choosing
# what the record will say, and a record of who did something is worth nothing
# if the doer wrote it.
#
# Over HTTP a credential identifies the caller at the edge and the principal is
# that user. Over stdio there is no caller to identify: the server is spawned
# over a pipe by the process it serves, and an agent did it.
CALLER_UNAUTHENTICATED = "agent"


def caller() -> str:
    """The identity this server writes into a record it makes."""
    from core.integrations.mcp.surface import current_caller

    return current_caller() or CALLER_UNAUTHENTICATED


def jdump(obj, indent=2):
    return json.dumps(obj, cls=_JsonEncoder, indent=indent)


def get_data_service():
    """Return the shared DatabaseDataService (demo mode or PostgreSQL)."""
    global _data_service
    if _data_service is None:
        from core.storage.database_data_service import DatabaseDataService

        _data_service = DatabaseDataService()
        backend_info = _data_service.get_backend_info()
        logger.info(f"MCP vigil using backend: {backend_info['backend']}")

    return _data_service


def load_findings():
    """Load findings from DatabaseDataService."""
    try:
        return get_data_service().get_findings(limit=10000)
    except Exception as e:
        logger.error(f"Error loading findings via DatabaseDataService: {e}")
        return []


@mcp.tool()
def list_findings(
    severity: Optional[str] = None,
    data_source: Optional[str] = None,
    cluster_id: Optional[str] = None,
    min_anomaly_score: Optional[float] = None,
    limit: int = 50,
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
def get_finding(finding_id: str) -> str:
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
) -> str:
    """Return completed threat-hunt projections for an assessment window."""
    try:
        return jdump(await pack_completed_hunts(start=start, end=end, limit=limit))
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
async def replay_hunt(run_id: str, decision_id: Optional[str] = None) -> str:
    """Rebuild what each decision of a completed hunt was shown (rebuilt, recorded,
    mismatch, recalled). ``decision_id`` narrows the report to one decision."""
    try:
        report = await read_replay(run_id, decision_id)
        if report is None:
            return jdump({"error": f"Nothing to replay for run {run_id}"})
        return jdump(report)
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def technique_rollup(min_confidence: float = 0.5) -> str:
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
) -> str:
    try:
        cases = get_data_service().get_cases()
        # Filtered here rather than in the query, which is what GET /api/v1/cases
        # does; doing it the other way would be a second answer to what "status"
        # means.
        if status:
            cases = [c for c in cases if c.get("status") == status]
        if priority:
            cases = [c for c in cases if c.get("priority") == priority]

        results = [
            {
                "case_id": c.get("case_id"),
                "title": c.get("title"),
                "status": c.get("status"),
                "priority": c.get("priority"),
                "assignee": c.get("assignee"),
                "finding_count": len(c.get("findings") or []),
                "created_at": c.get("created_at"),
                "updated_at": c.get("updated_at"),
            }
            for c in cases[:limit]
        ]
        return jdump({"total": len(results), "cases": results})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def get_case(case_id: str) -> str:
    try:
        case = get_data_service().get_case(case_id)
        if not case:
            return jdump({"error": f"Case {case_id} not found"})

        result = {
            key: case.get(key) or default
            for key, default in (
                ("case_id", None),
                ("title", None),
                ("description", None),
                ("status", None),
                ("priority", None),
                ("assignee", None),
                ("tags", []),
                ("notes", []),
                ("timeline", []),
                ("activities", []),
                ("resolution_steps", []),
                ("mitre_techniques", []),
                ("created_at", None),
                ("updated_at", None),
            )
        }
        findings = case.get("findings")
        if findings is not None:
            result["findings"] = [
                {
                    "finding_id": f.get("finding_id"),
                    "severity": f.get("severity"),
                    "data_source": f.get("data_source"),
                    "anomaly_score": float(f.get("anomaly_score") or 0),
                    "timestamp": f.get("timestamp"),
                    "status": f.get("status"),
                }
                for f in findings
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
) -> str:
    try:
        service = get_data_service()
        # The id is the service's to mint, so a Case opened through a tool and a
        # Case opened through the route are named the same way.
        case = service.create_case(
            title=title,
            finding_ids=finding_ids,
            priority=priority,
            description=description,
            status=status,
        )
        if not case:
            return jdump({"error": "Failed to create case"})

        # create_case carries what a Case is opened with; assignee and tags are
        # edits to one, and are applied as edits rather than by widening it.
        edits = {}
        if assignee is not None:
            edits["assignee"] = assignee
        if tags:
            edits["tags"] = tags
        if edits:
            service.update_case(case["case_id"], **edits)
            case = service.get_case(case["case_id"]) or case

        return jdump(
            {
                "success": True,
                "case_id": case.get("case_id"),
                "title": case.get("title"),
                "status": case.get("status"),
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

    Trust is `agent` unconditionally here. A credential on this surface is one a
    program holds, so even over HTTP what closed the Case is a program acting
    with someone's standing -- `closed_by` says whose, `closed_by_kind` says it
    was not them at a keyboard. `analyst` is the one record this system will not
    let an agent claim on its own behalf.
    """
    from core.cases.closure import ClosedByKind, ClosureCategory

    _close_through_the_service(
        case_id,
        closure_category=ClosureCategory.UNSPECIFIED,
        closed_by=caller(),
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
) -> str:
    try:
        service = get_data_service()
        case = service.get_case(case_id)
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
            notes = case.get("notes") or []
            notes.append({"timestamp": utcnow().isoformat() + "Z", "note": add_note})
            updates["notes"] = notes

        was_closed = (case.get("status") or "").strip() == "closed"
        if not service.update_case(case_id, **updates):
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
def add_finding_to_case(case_id: str, finding_id: str) -> str:
    try:
        from core.cases import case_journal_service

        linked = case_journal_service.link_finding(
            get_data_service(), case_id, finding_id
        )
        if linked is None:
            return jdump({"error": f"Failed to add {finding_id} to {case_id}"})
        return jdump(
            {
                "success": True,
                "message": (
                    f"Added {finding_id} to {case_id}"
                    if linked
                    else f"{finding_id} was already on {case_id}"
                ),
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def remove_finding_from_case(case_id: str, finding_id: str) -> str:
    try:
        from core.cases import case_journal_service

        unlinked = case_journal_service.unlink_finding(
            get_data_service(), case_id, finding_id
        )
        if unlinked is None:
            return jdump({"error": f"Failed to remove {finding_id} from {case_id}"})
        return jdump(
            {
                "success": True,
                "message": (
                    f"Removed {finding_id} from {case_id}"
                    if unlinked
                    else f"{finding_id} was not on {case_id}"
                ),
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_activity(
    case_id: str,
    activity_type: str,
    description: str,
    details: Optional[dict] = None,
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
        from core.cases import case_journal_service

        entry = case_journal_service.append_activity(
            get_data_service(),
            case_id,
            activity_type=activity_type,
            description=description,
            details=details,
        )
        if entry is None:
            return jdump({"error": f"Failed to add activity to {case_id}"})
        return jdump(
            {
                "success": True,
                "message": f"Added {activity_type} activity to {case_id}",
                "activity": entry,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_timeline_entry(
    case_id: str,
    event_description: str,
    event_time: Optional[str] = None,
    event_type: str = "investigation",
    details: Optional[dict] = None,
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
        from core.cases import case_journal_service

        entry = case_journal_service.append_timeline_entry(
            get_data_service(),
            case_id,
            event_description=event_description,
            event_time=event_time,
            event_type=event_type,
            details=details,
        )
        if entry is None:
            return jdump({"error": f"Failed to add timeline entry to {case_id}"})
        return jdump(
            {
                "success": True,
                "message": f"Added timeline entry to {case_id}",
                "entry": entry,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_mitre_techniques(case_id: str, technique_ids: list) -> str:
    """
    Add MITRE ATT&CK technique IDs to a case to document the kill chain.

    Args:
        case_id: The case ID
        technique_ids: List of MITRE technique IDs (e.g., ["T1071.001", "T1059.001"])

    Example:
        add_case_mitre_techniques("case-123", ["T1071.001", "T1059.001", "T1048.003"])
    """
    try:
        from core.cases import case_journal_service

        merged = case_journal_service.merge_mitre_techniques(
            get_data_service(), case_id, technique_ids
        )
        if merged is None:
            return jdump({"error": f"Failed to add techniques to {case_id}"})
        return jdump(
            {
                "success": True,
                "message": f"Added {len(merged['added'])} new techniques to {case_id}",
                "added_techniques": merged["added"],
                "all_techniques": merged["all"],
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_resolution_step(
    case_id: str,
    description: str,
    action_taken: str,
    result: Optional[str] = None,
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
        from core.cases import case_journal_service

        step = case_journal_service.append_resolution_step(
            get_data_service(),
            case_id,
            description=description,
            action_taken=action_taken,
            result=result,
        )
        if step is None:
            return jdump({"error": f"Failed to add resolution step to {case_id}"})
        return jdump(
            {
                "success": True,
                "message": f"Added resolution step to {case_id}",
                "step": step,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def bulk_add_findings_to_case(
    case_id: str, finding_ids: list, note: Optional[str] = None
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
        from core.cases import case_journal_service

        if not get_data_service().get_case(case_id):
            return jdump({"error": f"Case {case_id} not found"})

        added = []
        failed = []

        for finding_id in finding_ids:
            try:
                if case_journal_service.link_finding(
                    get_data_service(), case_id, finding_id
                ):
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
    content: str,
    parent_comment_id: Optional[int] = None,
) -> str:
    """
    Add a comment to a case. Supports threaded discussions.

    Args:
        case_id: The case ID
        content: Comment text
        parent_comment_id: Optional ID of parent comment for threading

    Examples:
        - add_case_comment("case-123", "Confirmed lateral movement pattern")
        - add_case_comment("case-123", "I see the same pattern", parent_comment_id=5)
    """
    try:
        from core.cases.case_collaboration_service import CaseCollaborationService

        with _service_session() as session:
            comment = CaseCollaborationService().add_comment(
                case_id=case_id,
                author=caller(),
                content=content,
                parent_comment_id=parent_comment_id,
                session=session,
            )
            if comment is None:
                return jdump({"error": f"Could not comment on {case_id}"})
            # The row is added, not yet flushed, so comment_id is unassigned
            # until the database supplies it.
            session.flush()
            payload = comment.to_dict()

        return jdump(
            {
                "success": True,
                "comment_id": payload.get("comment_id"),
                "message": f"Added comment to {case_id}",
                "comment": payload,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def get_case_comments(case_id: str) -> str:
    """Get all comments for a case."""
    try:
        from core.cases.case_collaboration_service import CaseCollaborationService

        with _service_session() as session:
            comments = CaseCollaborationService().get_case_comments(
                case_id=case_id, session=session
            )
            payload = [c.to_dict() for c in comments]

        return jdump(
            {
                "case_id": case_id,
                "comment_count": len(payload),
                "comments": payload,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def add_case_evidence(
    case_id: str,
    evidence_type: str,
    name: str,
    description: Optional[str] = None,
    file_path: Optional[str] = None,
    source: Optional[str] = None,
    tags: Optional[list] = None,
) -> str:
    """
    Add evidence to a case with chain of custody tracking.

    Args:
        case_id: The case ID
        evidence_type: Type (e.g., "file", "log", "network_capture", "memory_dump", "screenshot")
        name: Evidence name
        description: Optional description
        file_path: Optional file path
        source: Optional source system
        tags: Optional tags list

    Examples:
        - add_case_evidence("case-123", "memory_dump", "host-42-memory.raw",
                           description="Memory dump from compromised host")
        - add_case_evidence("case-123", "log", "firewall-logs.txt",
                           source="Palo Alto FW", tags=["c2", "exfiltration"])
    """
    try:
        from core.cases.case_evidence_service import CaseEvidenceService

        with _service_session() as session:
            evidence = CaseEvidenceService().add_evidence(
                case_id=case_id,
                evidence_type=evidence_type,
                name=name,
                collected_by=caller(),
                description=description,
                file_path=file_path,
                source=source,
                tags=tags,
                session=session,
            )
            if evidence is None:
                return jdump({"error": f"Could not add evidence to {case_id}"})
            session.flush()
            payload = evidence.to_dict()

        return jdump(
            {
                "success": True,
                "evidence_id": payload.get("evidence_id"),
                "message": f"Added evidence '{name}' to {case_id}",
                "evidence": payload,
            }
        )
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
        from core.cases.case_ioc_service import CaseIOCService

        with _service_session() as session:
            ioc = CaseIOCService().add_ioc(
                case_id=case_id,
                ioc_type=ioc_type,
                value=value,
                threat_level=threat_level,
                confidence=confidence,
                source=source,
                tags=tags,
                context=context,
                session=session,
            )
            if ioc is None:
                return jdump({"error": f"Could not add IOC {ioc_type}:{value}"})
            session.flush()
            payload = ioc.to_dict()

        return jdump(
            {
                "success": True,
                "ioc_id": payload.get("ioc_id"),
                "message": f"Added IOC {ioc_type}:{value} to {case_id}",
                "ioc": payload,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def bulk_add_iocs(case_id: str, iocs: list) -> str:
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
        from core.cases.case_ioc_service import CaseIOCService

        added = 0
        failed = 0
        results = []
        service = CaseIOCService()

        # One transaction for the batch. Calling the single-IOC tool in a loop
        # opened a transaction per indicator, so a batch could half-land.
        with _service_session() as session:
            for ioc_data in iocs:
                try:
                    ioc = service.add_ioc(
                        case_id=case_id,
                        ioc_type=ioc_data.get("ioc_type"),
                        value=ioc_data.get("value"),
                        threat_level=ioc_data.get("threat_level"),
                        confidence=ioc_data.get("confidence"),
                        source=ioc_data.get("source"),
                        tags=ioc_data.get("tags"),
                        context=ioc_data.get("context"),
                        session=session,
                    )
                    if ioc is None:
                        failed += 1
                        results.append({"error": "not added", "ioc": ioc_data})
                        continue
                    session.flush()
                    added += 1
                    results.append({"success": True, "ioc": ioc.to_dict()})
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
def get_case_iocs(case_id: str, ioc_type: Optional[str] = None) -> str:
    """Get all IOCs for a case, optionally filtered by type."""
    try:
        from core.cases.case_ioc_service import CaseIOCService

        with _service_session() as session:
            iocs = CaseIOCService().get_case_iocs(
                case_id=case_id, ioc_type=ioc_type, session=session
            )
            payload = [ioc.to_dict() for ioc in iocs]

        return jdump(
            {
                "case_id": case_id,
                "ioc_count": len(payload),
                "iocs": payload,
            }
        )
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

        from core.cases import case_records_service

        with _service_session() as session:
            task = case_records_service.add_task(
                session,
                case_id,
                title=title,
                description=description,
                assignee=assignee,
                priority=priority,
                due_date=(
                    datetime.fromisoformat(due_date.replace("Z", "+00:00"))
                    if due_date
                    else None
                ),
                checklist_items=None,
            )
            payload = task.to_dict()

        return jdump(
            {
                "success": True,
                "task_id": payload.get("task_id"),
                "message": f"Added task '{title}' to {case_id}",
                "task": payload,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def update_case_task(
    task_id: int,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    notes: Optional[str] = None,
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
        from core.cases import case_records_service

        updates = {"status": status, "assignee": assignee}
        if status == "completed":
            updates["completed_at"] = utcnow()

        with _service_session() as session:
            task = case_records_service.update_task(session, task_id, updates)
            if task is None:
                return jdump({"error": f"Task {task_id} not found"})
            payload = task.to_dict()
            case_id = task.case_id
            title = task.title

        # Activity is its own transaction, after the update has committed, so a
        # failure to record it cannot roll the update back.
        if status:
            add_case_activity(
                case_id,
                "task_update",
                f"Task '{title}' status changed to {status}",
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
                "task": payload,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def get_case_tasks(case_id: str) -> str:
    """Get all tasks for a case."""
    try:
        from core.cases import case_records_service

        tasks = case_records_service.list_tasks(case_id)
        payload = [t.to_dict() for t in tasks]

        return jdump(
            {
                "case_id": case_id,
                "task_count": len(payload),
                "tasks": payload,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def link_related_cases(
    case_id: str,
    related_case_id: str,
    relationship_type: str,
    notes: Optional[str] = None,
) -> str:
    """
    Link two related cases together.

    Args:
        case_id: Primary case ID
        related_case_id: Related case ID
        relationship_type: Type ("duplicate", "related", "parent", "child", "blocks", "blocked_by")
        notes: Optional notes about relationship

    Examples:
        - link_related_cases("case-123", "case-124", "related",
                            notes="Both cases show same attack pattern")
        - link_related_cases("case-123", "case-125", "parent",
                            notes="case-123 is the parent campaign")
    """
    try:
        from core.cases import case_records_service

        with _service_session() as session:
            relationship = case_records_service.add_relationship(
                session,
                case_id,
                related_case_id=related_case_id,
                relationship_type=relationship_type,
                created_by=caller(),
                notes=notes,
            )
            payload = relationship.to_dict()

        # After the link has committed, so a failure to note it cannot undo it.
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
                "relationship_id": payload.get("relationship_id"),
                "message": f"Linked {case_id} to {related_case_id} as {relationship_type}",
                "relationship": payload,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def escalate_case(
    case_id: str,
    escalated_to: str,
    reason: str,
    urgency_level: str = "high",
) -> str:
    """
    Escalate a case to higher tier or management.

    Args:
        case_id: The case ID
        escalated_to: Who to escalate to (username/team)
        reason: Reason for escalation
        urgency_level: Urgency ("low", "medium", "high", "critical")

    Example:
        escalate_case("case-123", "soc-manager",
                     "Suspected APT activity requires management approval",
                     urgency_level="critical")
    """
    try:
        from core.cases import case_records_service
        from core.cases.case_workflow_service import CaseWorkflowService

        with _service_session() as session:
            escalated = CaseWorkflowService().escalate_case(
                case_id=case_id,
                escalated_from=caller(),
                escalated_to=escalated_to,
                reason=reason,
                urgency_level=urgency_level,
                session=session,
            )
            if not escalated:
                return jdump({"error": f"Could not escalate {case_id}"})

            session.flush()
            # Read back the way POST /{case_id}/escalate does: the service
            # reports whether it escalated, not which row it wrote.
            escalations = case_records_service.list_escalations(session, case_id)
            payload = escalations[-1].to_dict() if escalations else {}

        add_case_activity(
            case_id,
            "escalation",
            f"Case escalated to {escalated_to}: {reason}",
            {
                "escalation_id": payload.get("escalation_id"),
                "urgency": urgency_level,
            },
        )

        return jdump(
            {
                "success": True,
                "escalation_id": payload.get("escalation_id"),
                "message": f"Escalated {case_id} to {escalated_to}",
                "escalation": payload,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def close_case(
    case_id: str,
    closure_category: str,
    root_cause: Optional[str] = None,
    lessons_learned: Optional[str] = None,
    recommendations: Optional[str] = None,
    executive_summary: Optional[str] = None,
    false_positive_reason: Optional[str] = None,
    closure_notes: Optional[str] = None,
) -> str:
    """
    Properly close a case with closure metadata.

    Args:
        case_id: The case ID
        closure_category: Category ("resolved", "false_positive", "duplicate", "unable_to_resolve")
        root_cause: Optional root cause analysis
        lessons_learned: Optional lessons learned
        recommendations: Optional recommendations
        executive_summary: Optional executive summary
        false_positive_reason: Optional reason a false-positive closure was one
        closure_notes: Optional free-text notes on the closure

    Example:
        close_case("case-123", "resolved",
                  root_cause="Compromised credentials due to phishing",
                  lessons_learned="Need MFA enforcement",
                  recommendations="Deploy MFA to all users, additional phishing training",
                  executive_summary="Lateral movement attack contained and remediated")
    """
    try:
        from core.cases.case_workflow_service import CaseWorkflowService
        from core.cases.closure import ClosedByKind, ClosureCategory

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

        with _service_session() as session:
            # Through the service rather than writing the rows here. This tool
            # had its own copy of the close, so the SLA clock, the IOC index and
            # anything added to a close later were the service's alone -- and a
            # case closed by an agent was a different shape of closed from one
            # closed through the API.
            closure = CaseWorkflowService().close_case(
                session,
                case_id,
                closure_category=category,
                closed_by=caller(),
                # A credential here is one a program holds, so this is a
                # program acting with someone's standing rather than that
                # person closing it. Episodic memory reads this as Trust, and
                # `analyst` is the one record this system will not let an agent
                # claim on its own behalf.
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

        # After the closure has committed, so a failure to note it cannot
        # leave a Case that closed and says nothing about it.
        add_case_activity(
            case_id,
            "case_closed",
            f"Case closed as {category.value}",
            {"closure_category": category.value, "closed_by": caller()},
        )

        return jdump(
            {
                "success": True,
                "message": f"Closed {case_id} as {category.value}",
                "closure": payload,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


# --- Approval queue -------------------------------------------------------
#
# These five tools were a second server, `approval`, speaking the low-level
# Server API over its own stdio pipe. They ask the same process for the same
# database as everything above, so they are tools on this server now. Names,
# arguments and JSON shape are unchanged: a caller that spoke to `approval`
# sees the same answers here.


def get_approval_svc():
    from core.response.approval_service import (
        ActionStatus,
        ActionType,
        get_approval_service,
    )

    return get_approval_service(), ActionType, ActionStatus


@mcp.tool()
def create_approval_action(
    action_type: str,
    title: str,
    description: str,
    target: str,
    confidence: float,
    reason: str,
    evidence: Optional[list] = None,
) -> str:
    """Submit action to approval queue.

    ``action_type`` is one of isolate_host, block_ip, block_domain,
    quarantine_file, disable_user, custom.

    ``evidence`` is optional here as it always was in practice: the old
    server declared it required in the schema and then accepted a call
    without it, so requiring it now would refuse calls that used to work.
    """
    try:
        svc, ActionType, ActionStatus = get_approval_svc()
    except Exception as e:
        return jdump({"error": f"Service error: {e}"})

    try:
        action = svc.create_action(
            action_type=ActionType(action_type),
            title=title,
            description=description,
            target=target,
            confidence=confidence,
            reason=reason,
            evidence=evidence or [],
            created_by=caller(),
        )
        msg = f"Action created. Status: {action.status}"
        if action.status == "approved":
            msg += f" (auto-approved, conf: {confidence:.0%})"
        return jdump(
            {
                "success": True,
                "action_id": action.action_id,
                "status": action.status,
                "message": msg,
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def list_approval_actions(
    status: Optional[str] = None,
    action_type: Optional[str] = None,
) -> str:
    """List approval actions.

    ``status`` is one of pending, approved, rejected, executed, failed.
    """
    try:
        svc, ActionType, ActionStatus = get_approval_svc()
    except Exception as e:
        return jdump({"error": f"Service error: {e}"})

    try:
        actions = svc.list_actions(
            status=ActionStatus(status) if status else None,
            action_type=ActionType(action_type) if action_type else None,
        )
        return jdump(
            {
                "success": True,
                "count": len(actions),
                "actions": [
                    {
                        "action_id": a.action_id,
                        "action_type": a.action_type,
                        "title": a.title,
                        "target": a.target,
                        "confidence": a.confidence,
                        "status": a.status,
                        "created_at": a.created_at,
                    }
                    for a in actions
                ],
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def get_approval_action(action_id: str) -> str:
    """Get action details."""
    try:
        svc, _ActionType, _ActionStatus = get_approval_svc()
    except Exception as e:
        return jdump({"error": f"Service error: {e}"})

    try:
        action = svc.get_action(action_id)
        if not action:
            return jdump({"error": f"Action {action_id} not found"})
        return jdump(
            {
                "success": True,
                "action": {
                    "action_id": action.action_id,
                    "action_type": action.action_type,
                    "title": action.title,
                    "description": action.description,
                    "target": action.target,
                    "confidence": action.confidence,
                    "reason": action.reason,
                    "evidence": action.evidence,
                    "status": action.status,
                    "created_at": action.created_at,
                    "approved_at": action.approved_at,
                    "approved_by": action.approved_by,
                },
            }
        )
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def approve_action(action_id: str) -> str:
    """Approve pending action."""
    try:
        svc, _ActionType, _ActionStatus = get_approval_svc()
    except Exception as e:
        return jdump({"error": f"Service error: {e}"})

    try:
        action = svc.approve_action(action_id, caller())
        if not action:
            return jdump({"error": f"Action {action_id} not found"})
        return jdump({"success": True, "action_id": action_id, "status": action.status})
    except Exception as e:
        return jdump({"error": str(e)})


@mcp.tool()
def reject_action(
    action_id: str,
    reason: str,
) -> str:
    """Reject pending action."""
    try:
        svc, _ActionType, _ActionStatus = get_approval_svc()
    except Exception as e:
        return jdump({"error": f"Service error: {e}"})

    try:
        action = svc.reject_action(action_id, reason, caller())
        if not action:
            return jdump({"error": f"Action {action_id} not found"})
        return jdump({"success": True, "action_id": action_id, "status": action.status})
    except Exception as e:
        return jdump({"error": str(e)})


if __name__ == "__main__":
    mcp.run()
