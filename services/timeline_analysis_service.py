"""Timeline event AI analysis (relocated from ClaudeService for #413 4c-4).

This module owns the timeline endpoint's single AI method,
``generate_event_analysis``, which used to live on ``ClaudeService``. It is
provider-agnostic: it dispatches through ``LLMRouter.chat`` with no explicit
provider, so the analysis runs on whichever provider is configured as the
default (Anthropic, OpenAI, or a local Ollama via Bifrost) rather than being
hard-wired to the Anthropic SDK. Keeping it here — the timeline's only caller —
means ``backend/api/timeline.py`` no longer imports ``ClaudeService``, which is
the #413 boundary end-state.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from services.defaults import DEFAULT_MODEL

logger = logging.getLogger(__name__)


async def generate_event_analysis(
    event_data: Dict,
    related_events: List[Dict],
    finding_data: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Generate comprehensive incident analysis for a timeline event.

    This provides AI-powered analysis for SOC analysts to quickly understand
    security events in context.

    Args:
        event_data: The main event data
        related_events: List of related events in the time window
        finding_data: Optional associated finding data

    Returns:
        Dictionary with analysis fields:
        - incident_summary: Plain language summary of what happened
        - attack_narrative: Story of the attack based on event sequence
        - entity_analysis: Explanation of entity relationships
        - threat_assessment: Risk level and severity justification
        - investigation_priorities: What to investigate next
        - response_recommendations: Immediate recommended actions
        - timeline_correlation: How this event fits in the timeline
        - confidence_score: Confidence in the analysis (0.0-1.0)
    """
    system_prompt = """You are an expert SOC analyst providing incident analysis for timeline events.

Your analysis should help SOC analysts quickly understand:
- What happened in this security event
- How it relates to other events
- What entities (IPs, hosts, users) are involved
- What threat it represents
- What to investigate next
- What actions to take

Provide clear, actionable analysis in JSON format. Be concise but thorough.
Focus on practical insights that help with investigation and response."""

    # Prepare event context
    event_time = event_data.get("start", "")
    event_type = event_data.get("type", "unknown")
    event_severity = event_data.get("severity", "unknown")

    # Build context about entities (handles both singular and plural field formats)
    entities_summary = ""
    if finding_data and finding_data.get("entity_context"):
        entity_ctx = finding_data["entity_context"]
        entities_list = []
        src_ips = entity_ctx.get("src_ips") or []
        if not src_ips and entity_ctx.get("src_ip"):
            src_ips = [entity_ctx["src_ip"]]
        dst_ips = entity_ctx.get("dst_ips") or entity_ctx.get("dest_ips") or []
        if not dst_ips and entity_ctx.get("dst_ip"):
            dst_ips = [entity_ctx["dst_ip"]]
        hostnames = entity_ctx.get("hostnames") or []
        if not hostnames and entity_ctx.get("hostname"):
            hostnames = [entity_ctx["hostname"]]
        users = entity_ctx.get("users") or entity_ctx.get("usernames") or []
        if not users and entity_ctx.get("user"):
            users = [entity_ctx["user"]]
        if src_ips:
            entities_list.append(
                f"Source IPs: {', '.join(str(ip) for ip in src_ips[:5])}"
            )
        if dst_ips:
            entities_list.append(
                f"Destination IPs: {', '.join(str(ip) for ip in dst_ips[:5])}"
            )
        if hostnames:
            entities_list.append(f"Hosts: {', '.join(str(h) for h in hostnames[:5])}")
        if users:
            entities_list.append(f"Users: {', '.join(str(u) for u in users[:5])}")
        entities_summary = "\n".join(entities_list)

    # Build related events context
    related_summary = ""
    if related_events:
        related_summary = f"\n{len(related_events)} related events in time window:\n"
        for i, re in enumerate(related_events[:10], 1):
            re_time = re.get("start", "")
            re_sev = re.get("severity", "unknown")
            re_content = re.get("content", "")[:100]
            related_summary += f"{i}. [{re_sev}] {re_time} - {re_content}\n"

    # Build finding context
    finding_summary = ""
    if finding_data:
        desc = finding_data.get("description") or "N/A"
        finding_summary = f"""
Associated Finding:
- ID: {finding_data.get('finding_id') or 'N/A'}
- Severity: {finding_data.get('severity') or 'unknown'}
- Data Source: {finding_data.get('data_source') or 'unknown'}
- Anomaly Score: {float(finding_data.get('anomaly_score') or 0)}
- Description: {desc[:200]}
"""
        mitre_preds = finding_data.get("mitre_predictions") or {}
        if mitre_preds:
            top_techniques = sorted(
                mitre_preds.items(), key=lambda x: float(x[1] or 0), reverse=True
            )[:3]
            finding_summary += f"\nTop MITRE Techniques: {', '.join([f'{t[0]} ({float(t[1] or 0):.2f})' for t in top_techniques])}"

    prompt = f"""Analyze this security event and provide comprehensive incident analysis.

EVENT DETAILS:
- Time: {event_time}
- Type: {event_type}
- Severity: {event_severity}
- Content: {event_data.get('content', '')}

{finding_summary}

ENTITIES INVOLVED:
{entities_summary if entities_summary else 'No entity information available'}

RELATED EVENTS:
{related_summary if related_summary else 'No related events in time window'}

Provide analysis in the following JSON format:
{{
  "incident_summary": "2-3 sentence plain language summary of what happened",
  "attack_narrative": "Story explaining the attack sequence and progression",
  "entity_analysis": "Explanation of how entities are connected and their roles",
  "threat_assessment": "Risk level assessment and severity justification",
  "investigation_priorities": ["Priority 1", "Priority 2", "Priority 3"],
  "response_recommendations": ["Action 1", "Action 2", "Action 3"],
  "timeline_correlation": "How this event fits in the bigger picture",
  "confidence_score": 0.85
}}

Provide only the JSON, no additional text."""

    try:
        # Provider-agnostic dispatch (#413): no explicit provider, so the router
        # resolves the configured default. service_config mirrors the old
        # ClaudeService(use_backend_tools=True, use_mcp_tools=False) construction
        # on the Anthropic engine path.
        from services.llm_router import LLMRouter

        response = await LLMRouter().chat(
            prompt,
            system_prompt=system_prompt,
            model=DEFAULT_MODEL,
            service_config={"use_backend_tools": True, "use_mcp_tools": False},
        )

        # Parse JSON response. The model might wrap it in markdown code blocks,
        # so handle that. A None/empty reply falls through to the JSON parse and
        # is handled by the JSONDecodeError fallback below.
        response_text = (response or "").strip()
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        response_text = response_text.strip()

        analysis = json.loads(response_text)

        # Validate required fields
        required_fields = [
            "incident_summary",
            "attack_narrative",
            "entity_analysis",
            "threat_assessment",
            "investigation_priorities",
            "response_recommendations",
            "timeline_correlation",
        ]
        for field in required_fields:
            if field not in analysis:
                analysis[field] = f"Analysis for {field} not available"

        if "confidence_score" not in analysis:
            analysis["confidence_score"] = 0.7

        return analysis

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse event analysis JSON: {e}")
        # Return fallback analysis
        return {
            "incident_summary": "AI analysis generated but could not be parsed properly.",
            "attack_narrative": "Event analysis is available but needs manual review.",
            "entity_analysis": "Entity relationships detected in event data.",
            "threat_assessment": f"Event severity: {event_severity}",
            "investigation_priorities": [
                "Review event details",
                "Check entity context",
                "Correlate with related events",
            ],
            "response_recommendations": [
                "Investigate further",
                "Monitor related systems",
                "Review security logs",
            ],
            "timeline_correlation": "Event occurred in the specified time window with related security events.",
            "confidence_score": 0.5,
            "error": "JSON parsing failed",
        }
    except Exception as e:
        logger.error(f"Error generating event analysis: {e}")
        raise
