"""Workflow selection and initial workdir files for autonomous investigations."""

from typing import Any, Dict, List, Optional

from core.documents.fence import fenced_document
from core.time import utcnow
from core.workflows.routing import FALLBACK_WORKFLOW


def select_workflow(finding: Dict[str, Any]) -> str:
    """Select the most appropriate workflow for a finding based on its characteristics."""
    severity = (finding.get("severity") or "").lower()
    recommended = (finding.get("recommended_action") or "").lower()
    category = (finding.get("category") or "").lower()
    mitre = finding.get("mitre_predictions") or {}

    if recommended in ("isolate", "block") or severity == "critical":
        return FALLBACK_WORKFLOW

    if category in ("malware", "ransomware"):
        return "forensic-analysis"

    if len(mitre) >= 3:
        return "full-investigation"

    if severity == "high":
        return "full-investigation"

    return FALLBACK_WORKFLOW


def _infer_title(finding: Dict[str, Any], workflow_id: str) -> str:
    mitre = finding.get("mitre_predictions") or {}
    desc = finding.get("description") or ""
    ctx = finding.get("entity_context") or {}

    subject_parts = []
    hostnames = ctx.get("hostnames") or (
        [ctx["hostname"]] if ctx.get("hostname") else []
    )
    if hostnames:
        subject_parts.append(hostnames[0])

    src_ips = ctx.get("src_ips") or ([ctx["src_ip"]] if ctx.get("src_ip") else [])
    if src_ips and not subject_parts:
        subject_parts.append(src_ips[0])

    subject = subject_parts[0] if subject_parts else "Unknown Entity"

    if mitre:
        top_technique = max(mitre, key=mitre.get)
        return f"Suspicious Activity on {subject} ({top_technique})"

    if desc:
        short = desc[:60].rstrip()
        if len(desc) > 60:
            short += "..."
        return short

    workflow_names = {
        "incident-response": "Incident Response",
        "full-investigation": "Full Investigation",
        "threat-hunt": "Threat Hunt",
        "forensic-analysis": "Forensic Analysis",
    }
    return f"{workflow_names.get(workflow_id, 'Investigation')} - {subject}"


def generate_initial_state(
    investigation_id: str,
    workflow_id: str,
    case_id: Optional[str],
    findings: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Generate the initial state.json for an investigation."""
    return {
        "investigation_id": investigation_id,
        "workflow_id": workflow_id,
        "case_id": case_id,
        "status": "executing",
        "current_step": 1,
        "trigger_finding_ids": [
            f.get("finding_id") for f in findings if f.get("finding_id")
        ],
        "created_at": utcnow().isoformat(),
        "discovered_iocs": {},
        "discovered_entities": {},
        "proposed_actions": [],
        "blockers": [],
    }


def generate_initial_context(
    findings: List[Dict[str, Any]],
    case_id: Optional[str] = None,
    document: Optional[str] = None,
) -> str:
    """Generate the initial context.md with trigger finding summaries.

    ``document`` is whatever a person handed the ask -- a URL, a path, or the
    report text. It goes in whole and unparsed, ahead of the findings: a run
    never reads the trigger payload, so the brief is the only way it arrives.
    It is fenced, because a pasted third-party report is written by someone
    other than the analyst, and its own headings or instructions must not read
    as the brief's.
    """
    lines = ["# Investigation Context", ""]
    if case_id:
        lines.extend([f"case_id: {case_id}", ""])
    if document:
        lines.extend(["## Attached Document", "", *fenced_document(document), ""])
    lines.extend(["## Trigger Findings", ""])

    for f in findings[:5]:
        fid = f.get("finding_id", "unknown")
        sev = f.get("severity", "unknown")
        desc = (f.get("description") or "No description")[:200]
        lines.append(f"### {fid} (Severity: {sev})")
        lines.append(desc)
        lines.append("")

    lines.append("## Progress Notes")
    lines.append("")

    return "\n".join(lines)
