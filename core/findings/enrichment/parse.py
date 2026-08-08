"""Response parsing for finding enrichment.

Local models in particular wrap their JSON in markdown fences or trail prose
after it, so extraction is forgiving and falls back to a synthesized payload
rather than failing the request.
"""

import json
import re
from typing import Any, Dict, Optional

from core.findings.enrichment.errors import EmptyProviderResponse

_FENCED_JSON = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_RAW_JSON = re.compile(r"\{.*\}", re.DOTALL)


def extract_json_block(response: str) -> str:
    """Return the most likely JSON substring of ``response``.

    Prefers a ```json fenced block, then the first ``{...}`` span, then the
    whole response.
    """
    fenced = _FENCED_JSON.search(response)
    if fenced:
        return fenced.group(1)
    raw = _RAW_JSON.search(response)
    if raw:
        return raw.group(0)
    return response


def parse_enrichment(response: Optional[str], *, severity: str) -> Dict[str, Any]:
    """Parse a provider response into an enrichment payload.

    Raises:
        EmptyProviderResponse: the provider returned nothing to parse.
    """
    if not response:
        raise EmptyProviderResponse("LLM provider returned an empty response")

    try:
        return json.loads(extract_json_block(response))
    except json.JSONDecodeError:
        # Unparseable output still carries analyst value, so synthesize a
        # payload with the same shape the UI renders and park the raw text in
        # analysis_notes rather than 500-ing the request.
        return {
            "threat_summary": "AI analysis completed - see full analysis below",
            "threat_type": "Security Finding",
            "potential_impact": "Requires manual review",
            "risk_level": severity.title() if severity else "Medium",
            "recommended_actions": [
                "Review the detailed analysis",
                "Investigate related entities",
            ],
            "investigation_questions": [
                "What is the root cause?",
                "Are there related events?",
            ],
            "indicators": {},
            "related_techniques": [],
            "timeline_context": "Analysis in progress",
            "business_context": "Requires additional context",
            "confidence_score": 0.7,
            "analysis_notes": response[:1000],  # first 1000 chars as notes
            "raw_response": response,  # full response
        }
