"""Input shaping and prompt construction for finding enrichment.

Pure functions — no provider, no database, no HTTP.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class FindingSummary:
    """The subset of a finding the enrichment prompt renders.

    ``severity`` is carried through because the parse-failure fallback in
    :mod:`core.findings.enrichment.parse` reuses it as ``risk_level``.
    """

    finding_id: str
    severity: str
    data_source: str
    timestamp: str
    description: str
    anomaly_score: float
    entity_str: str
    techniques_str: str


def build_entity_string(entity_context: Optional[Dict[str, Any]]) -> str:
    """Render entity context as prompt lines.

    Handles both singular and plural field formats: producers disagree on
    whether the key is ``src_ip`` or ``src_ips`` (and ``dst_ips`` vs
    ``dest_ips``, ``users`` vs ``usernames``), so accept every spelling.
    """
    entity_str = ""
    if not entity_context:
        return entity_str

    src_ips = entity_context.get("src_ips") or []
    if not src_ips and entity_context.get("src_ip"):
        src_ips = [entity_context["src_ip"]]
    dst_ips = entity_context.get("dst_ips") or entity_context.get("dest_ips") or []
    if not dst_ips and entity_context.get("dst_ip"):
        dst_ips = [entity_context["dst_ip"]]
    hostnames = entity_context.get("hostnames") or []
    if not hostnames and entity_context.get("hostname"):
        hostnames = [entity_context["hostname"]]
    users = entity_context.get("users") or entity_context.get("usernames") or []
    if not users and entity_context.get("user"):
        users = [entity_context["user"]]

    if src_ips:
        entity_str += f"Source IPs: {', '.join(str(ip) for ip in src_ips[:5])}\n"
    if dst_ips:
        entity_str += f"Destination IPs: {', '.join(str(ip) for ip in dst_ips[:5])}\n"
    if hostnames:
        entity_str += f"Hostnames: {', '.join(str(h) for h in hostnames[:5])}\n"
    if users:
        entity_str += f"Users: {', '.join(str(u) for u in users[:5])}\n"

    return entity_str


def build_techniques_string(
    predicted_techniques: Optional[List[Dict[str, Any]]],
    mitre_predictions: Optional[Dict[str, Any]],
) -> str:
    """Render up to five MITRE techniques as prompt lines.

    ``predicted_techniques`` (already ranked by the producer) wins when
    present; otherwise fall back to ``mitre_predictions``, sorted by
    confidence descending.
    """
    if predicted_techniques:
        return "\n".join(
            f"{t.get('technique_id', 'Unknown')} "
            f"(confidence: {float(t.get('confidence') or 0):.2f})"
            for t in predicted_techniques[:5]
        )
    if mitre_predictions:
        ranked = sorted(
            mitre_predictions.items(),
            key=lambda x: float(x[1] or 0),
            reverse=True,
        )[:5]
        return "\n".join(
            f"{tech_id} (confidence: {float(conf or 0):.2f})"
            for tech_id, conf in ranked
        )
    return ""


def summarize_finding(
    finding: Dict[str, Any], *, finding_id: Optional[str] = None
) -> FindingSummary:
    """Extract the prompt inputs from a finding dict.

    Uses ``or`` rather than ``dict.get`` defaults throughout: these keys are
    frequently *present with a None value*, which a plain default wouldn't
    catch.

    ``finding_id`` overrides the dict's own key. The HTTP handler passes the
    path param — its authoritative id — rather than trusting the row it just
    read back, which is what the pre-extraction handler did.
    """
    return FindingSummary(
        finding_id=finding_id or finding.get("finding_id") or "",
        severity=finding.get("severity") or "unknown",
        data_source=finding.get("data_source") or "unknown",
        timestamp=finding.get("timestamp") or "",
        description=finding.get("description") or "",
        anomaly_score=float(finding.get("anomaly_score") or 0),
        entity_str=build_entity_string(finding.get("entity_context") or {}),
        techniques_str=build_techniques_string(
            finding.get("predicted_techniques") or [],
            finding.get("mitre_predictions") or {},
        ),
    )


def build_prompt(summary: FindingSummary) -> str:
    """Build the enrichment analysis prompt for ``summary``."""
    finding_id = summary.finding_id
    severity = summary.severity
    data_source = summary.data_source
    timestamp = summary.timestamp
    description = summary.description
    anomaly_score = summary.anomaly_score
    entity_str = summary.entity_str
    techniques_str = summary.techniques_str

    return f"""You are a cybersecurity analyst reviewing a security finding. Provide a comprehensive, structured analysis.

FINDING DETAILS:
=================
Finding ID: {finding_id}
Severity: {severity}
Data Source: {data_source}
Timestamp: {timestamp}
Anomaly Score: {anomaly_score:.2f}

Description:
{description if description else 'No description available'}

{f'''Entity Context:
{entity_str}''' if entity_str else ''}

{f'''MITRE ATT&CK Techniques:
{techniques_str}''' if techniques_str else 'No MITRE techniques predicted'}

ANALYSIS REQUIREMENTS:
=======================
Please provide a detailed analysis in the following JSON structure:

{{
    "threat_summary": "A clear, concise summary (2-3 sentences) of what this finding represents and why it matters",
    "threat_type": "Classification of threat (e.g., 'Data Exfiltration', 'Lateral Movement', 'Command & Control', 'Malware', etc.)",
    "potential_impact": "Detailed explanation of potential impact on the organization (3-4 sentences)",
    "risk_level": "Overall risk assessment: 'Critical', 'High', 'Medium', or 'Low'",
    "recommended_actions": [
        "Immediate action item 1",
        "Immediate action item 2",
        "Additional investigation step 1",
        "Additional investigation step 2"
    ],
    "investigation_questions": [
        "Key question to investigate 1?",
        "Key question to investigate 2?",
        "Key question to investigate 3?"
    ],
    "indicators": {{
        "malicious_ips": ["list any suspicious IPs mentioned"],
        "suspicious_domains": ["list any suspicious domains"],
        "suspicious_users": ["list any suspicious user accounts"],
        "suspicious_processes": ["list any suspicious processes or commands"]
    }},
    "related_techniques": [
        {{
            "technique_id": "T####.###",
            "technique_name": "Technique name",
            "relevance": "Why this technique is relevant"
        }}
    ],
    "timeline_context": "Brief explanation of what likely happened and in what order",
    "business_context": "How this finding relates to typical business operations and what makes it anomalous",
    "confidence_score": 0.85,
    "analysis_notes": "Any additional context, caveats, or recommendations for the analyst"
}}

Respond ONLY with valid JSON. Be specific and actionable. Focus on helping a SOC analyst make quick, informed decisions."""
