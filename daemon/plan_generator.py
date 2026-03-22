"""Plan generator for autonomous investigations.

Converts workflow definitions and finding context into structured plan.md
files that sub-agents consume and modify during execution.
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

WORKFLOW_STEP_MAP = {
    "incident-response": [
        {"title": "Initial Triage", "description": "Retrieve finding details, assess severity, determine if false positive"},
        {"title": "Deep Investigation", "description": "Gather evidence, correlate related findings, build entity timeline"},
        {"title": "Correlate & Enrich", "description": "Search for related activity, enrich IOCs with threat intel"},
        {"title": "Map to MITRE ATT&CK", "description": "Map discovered TTPs, create ATT&CK Navigator layer"},
        {"title": "Containment & Response", "description": "Assess blast radius, propose containment actions, create approval requests"},
        {"title": "Case Management", "description": "Check existing cases via list_cases; add findings to matching case or create new case; log IOCs, timeline, and MITRE techniques to the case"},
        {"title": "Document & Report", "description": "Write investigation summary, submit for review"},
    ],
    "full-investigation": [
        {"title": "Evidence Gathering", "description": "Retrieve all related findings, entity context, and evidence"},
        {"title": "Deep Analysis", "description": "Analyze patterns, identify root cause, build hypotheses"},
        {"title": "ATT&CK Mapping", "description": "Map all TTPs to MITRE ATT&CK, identify technique chains"},
        {"title": "Cross-Signal Correlation", "description": "Correlate across data sources, find related campaigns"},
        {"title": "Response Planning", "description": "Determine response actions, assess risk, create approval requests"},
        {"title": "Case Management", "description": "Check existing cases via list_cases; add findings to matching case or create new case; log IOCs, timeline, and MITRE techniques to the case"},
        {"title": "Final Report", "description": "Comprehensive report with findings, timeline, and recommendations"},
    ],
    "threat-hunt": [
        {"title": "Hypothesis Formation", "description": "Form hunt hypothesis based on available intelligence"},
        {"title": "Data Collection", "description": "Gather relevant findings, search for matching patterns"},
        {"title": "Network Analysis", "description": "Analyze network flows, identify anomalous connections"},
        {"title": "Artifact Analysis", "description": "Examine suspicious artifacts, file hashes, processes"},
        {"title": "Intelligence Enrichment", "description": "Enrich discovered IOCs, check threat intel feeds"},
        {"title": "Case Management", "description": "Check existing cases via list_cases; add findings to matching case or create new case; log IOCs, timeline, and MITRE techniques to the case"},
        {"title": "Hunt Report", "description": "Document findings, update detections, submit report"},
    ],
    "forensic-analysis": [
        {"title": "Evidence Acquisition", "description": "Identify and preserve digital evidence, establish chain of custody"},
        {"title": "Initial Assessment", "description": "Preliminary analysis of evidence scope and key artifacts"},
        {"title": "Malware Analysis", "description": "Analyze suspicious files, executables, and scripts"},
        {"title": "Network Forensics", "description": "Examine network traffic, DNS logs, connection patterns"},
        {"title": "Timeline Reconstruction", "description": "Build comprehensive event timeline from all sources"},
        {"title": "Case Management", "description": "Check existing cases via list_cases; add findings to matching case or create new case; log IOCs, timeline, and MITRE techniques to the case"},
        {"title": "Forensic Report", "description": "Detailed forensic report with evidence chain and conclusions"},
    ],
    "case-review": [
        {"title": "Review Findings", "description": "Use get_case to load the case, then get_finding for each finding; review IOCs, timeline, and activities already logged"},
        {"title": "Root Cause Analysis", "description": "Determine root cause from aggregated evidence across all findings; identify the initial access vector and attack chain"},
        {"title": "Resolution Planning", "description": "Generate concrete resolution steps using add_resolution_step for containment, eradication, and recovery actions"},
        {"title": "Recommendations", "description": "Write preventive recommendations, lessons learned, and update case description with executive summary using update_case"},
        {"title": "Finalize Case", "description": "Ensure all resolution steps are recorded, case description updated, and signal_complete"},
    ],
}

DEFAULT_STEPS = [
    {"title": "Initial Assessment", "description": "Retrieve and assess the triggering finding(s)"},
    {"title": "Investigation", "description": "Gather evidence, correlate related activity"},
    {"title": "Analysis", "description": "Analyze patterns, enrich IOCs, map TTPs"},
    {"title": "Response", "description": "Propose containment and response actions"},
    {"title": "Case Management", "description": "Check existing cases via list_cases; add findings to matching case or create new case; log IOCs, timeline, and MITRE techniques to the case"},
    {"title": "Report", "description": "Document findings and submit for review"},
]


def select_workflow(finding: Dict[str, Any]) -> str:
    """Select the most appropriate workflow for a finding based on its characteristics."""
    severity = (finding.get("severity") or "").lower()
    recommended = (finding.get("recommended_action") or "").lower()
    category = (finding.get("category") or "").lower()
    mitre = finding.get("mitre_predictions") or {}
    
    if recommended in ("isolate", "block") or severity == "critical":
        return "incident-response"
    
    if category in ("malware", "ransomware"):
        return "forensic-analysis"
    
    if len(mitre) >= 3:
        return "full-investigation"
    
    if severity == "high":
        return "full-investigation"
    
    return "incident-response"


def _build_entity_section(finding: Dict[str, Any]) -> str:
    """Build the entity context section from a finding."""
    ctx = finding.get("entity_context") or {}
    parts = []
    
    src_ips = ctx.get("src_ips") or ([ctx["src_ip"]] if ctx.get("src_ip") else [])
    dst_ips = ctx.get("dest_ips") or ctx.get("dst_ips") or ([ctx["dst_ip"]] if ctx.get("dst_ip") else [])
    hostnames = ctx.get("hostnames") or ([ctx["hostname"]] if ctx.get("hostname") else [])
    users = ctx.get("usernames") or ctx.get("users") or ([ctx["user"]] if ctx.get("user") else [])
    domains = ctx.get("domains") or []
    hashes = ctx.get("file_hashes") or []
    
    if src_ips:
        parts.append(f"- Source IPs: {', '.join(src_ips[:5])}")
    if dst_ips:
        parts.append(f"- Destination IPs: {', '.join(dst_ips[:5])}")
    if hostnames:
        parts.append(f"- Hostnames: {', '.join(hostnames[:5])}")
    if users:
        parts.append(f"- Users: {', '.join(users[:5])}")
    if domains:
        parts.append(f"- Domains: {', '.join(domains[:5])}")
    if hashes:
        parts.append(f"- File Hashes: {', '.join(hashes[:3])}")
    
    return "\n".join(parts) if parts else "- No entity context available"


def _format_mitre(finding: Dict[str, Any]) -> str:
    mitre = finding.get("mitre_predictions") or {}
    if not mitre:
        return "None detected"
    techniques = []
    for tid, score in sorted(mitre.items(), key=lambda x: -x[1])[:5]:
        techniques.append(f"{tid} (confidence: {score:.2f})")
    return ", ".join(techniques)


def generate_plan(
    investigation_id: str,
    workflow_id: str,
    findings: List[Dict[str, Any]],
    case_id: Optional[str] = None,
    hypothesis: Optional[str] = None,
) -> str:
    """Generate a plan.md for an investigation."""
    steps = WORKFLOW_STEP_MAP.get(workflow_id, DEFAULT_STEPS)
    primary = findings[0] if findings else {}

    title = _infer_title(primary, workflow_id)

    lines = [
        "---",
        f"investigation_id: {investigation_id}",
        f"case_id: {case_id or 'pending'}",
        f"workflow: {workflow_id}",
        f"priority: {primary.get('severity', 'medium')}",
        f"created: {datetime.utcnow().isoformat()}Z",
        "status: planning",
        f"current_step: 1",
        "---",
        "",
        f"# Investigation Plan: {title}",
        "",
        "## Objective",
    ]
    
    if hypothesis:
        lines.append(f"Hunt hypothesis: {hypothesis}")
        lines.append("")
    
    if primary:
        fid = primary.get("finding_id", "unknown")
        desc = (primary.get("description") or "No description")[:300]
        lines.append(f"Investigate finding {fid}: {desc}")
        lines.append("")
        lines.append("### Entity Context")
        lines.append(_build_entity_section(primary))
        lines.append("")
        lines.append(f"### MITRE ATT&CK Predictions")
        lines.append(f"- {_format_mitre(primary)}")
        lines.append(f"- Anomaly Score: {primary.get('anomaly_score', 'N/A')}")
    else:
        lines.append("Investigate based on provided context.")
    
    if len(findings) > 1:
        lines.append("")
        lines.append(f"### Additional Trigger Findings ({len(findings) - 1})")
        for f in findings[1:5]:
            lines.append(f"- {f.get('finding_id', '?')}: {(f.get('description') or 'N/A')[:100]}")
    
    lines.append("")
    lines.append("## Steps")
    lines.append("")
    
    for i, step in enumerate(steps, 1):
        lines.append(f"### Step {i}: {step['title']} [pending]")
        lines.append(f"- {step['description']}")
        lines.append("")
    
    lines.append("## Blockers")
    lines.append("(none)")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    
    return "\n".join(lines)


def generate_case_review_plan(
    investigation_id: str,
    case_id: str,
    case_title: str,
    finding_ids: List[str],
    priority: str = "medium",
) -> str:
    """Generate a plan.md for a case-review investigation."""
    steps = WORKFLOW_STEP_MAP["case-review"]

    lines = [
        "---",
        f"investigation_id: {investigation_id}",
        f"case_id: {case_id}",
        "workflow: case-review",
        f"priority: {priority}",
        f"created: {datetime.utcnow().isoformat()}Z",
        "status: planning",
        "current_step: 1",
        "---",
        "",
        f"# Case Review Plan: {case_title}",
        "",
        "## Objective",
        f"Review case {case_id} and generate resolution steps, root cause analysis,",
        "and recommendations based on all findings and investigation results.",
        "",
        f"### Associated Findings ({len(finding_ids)})",
    ]

    for fid in finding_ids[:10]:
        lines.append(f"- {fid}")
    if len(finding_ids) > 10:
        lines.append(f"- ... and {len(finding_ids) - 10} more")

    lines.append("")
    lines.append("## Steps")
    lines.append("")

    for i, step in enumerate(steps, 1):
        lines.append(f"### Step {i}: {step['title']} [pending]")
        lines.append(f"- {step['description']}")
        lines.append("")

    lines.append("## Blockers")
    lines.append("(none)")
    lines.append("")
    lines.append("## Notes")
    lines.append("")

    return "\n".join(lines)


def generate_case_review_context(case_id: str, case_title: str, finding_ids: List[str]) -> str:
    """Generate the initial context.md for a case-review investigation."""
    lines = [
        "# Case Review Context",
        "",
        f"## Case: {case_id}",
        f"**Title:** {case_title}",
        "",
        f"## Findings to Review ({len(finding_ids)})",
        "",
    ]
    for fid in finding_ids[:10]:
        lines.append(f"- {fid}")
    if len(finding_ids) > 10:
        lines.append(f"- ... and {len(finding_ids) - 10} more")
    lines.append("")
    lines.append("## Progress Notes")
    lines.append("")
    return "\n".join(lines)


def _infer_title(finding: Dict[str, Any], workflow_id: str) -> str:
    mitre = finding.get("mitre_predictions") or {}
    desc = finding.get("description") or ""
    ctx = finding.get("entity_context") or {}

    subject_parts = []
    hostnames = ctx.get("hostnames") or ([ctx["hostname"]] if ctx.get("hostname") else [])
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
    total_steps: int,
) -> Dict[str, Any]:
    """Generate the initial state.json for an investigation."""
    return {
        "investigation_id": investigation_id,
        "workflow_id": workflow_id,
        "case_id": case_id,
        "status": "executing",
        "current_step": 1,
        "total_steps": total_steps,
        "trigger_finding_ids": [f.get("finding_id") for f in findings if f.get("finding_id")],
        "created_at": datetime.utcnow().isoformat(),
        "completed_steps": [],
        "discovered_iocs": {},
        "discovered_entities": {},
        "proposed_actions": [],
        "blockers": [],
    }


def generate_initial_context(findings: List[Dict[str, Any]]) -> str:
    """Generate the initial context.md with trigger finding summaries."""
    lines = ["# Investigation Context", "", "## Trigger Findings", ""]
    
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


def count_steps(workflow_id: str) -> int:
    return len(WORKFLOW_STEP_MAP.get(workflow_id, DEFAULT_STEPS))
