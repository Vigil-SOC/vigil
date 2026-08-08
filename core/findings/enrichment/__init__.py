"""Finding AI-enrichment: prompt building, provider dispatch, response parsing.

``enrich()`` is the entry point. It persists by default; pass ``persist=False``
to compose your own write. Failures are domain exceptions from ``errors``, never
``HTTPException``.
"""

from core.findings.enrichment.errors import (
    EmptyProviderResponse,
    EnrichmentError,
    FindingNotFound,
    NoProviderConfigured,
    ProviderUnavailable,
    UnidentifiableFinding,
)
from core.findings.enrichment.parse import extract_json_block, parse_enrichment
from core.findings.enrichment.prompt import (
    FindingSummary,
    build_entity_string,
    build_prompt,
    build_techniques_string,
    summarize_finding,
)
from core.findings.enrichment.service import enrich

__all__ = [
    "EmptyProviderResponse",
    "EnrichmentError",
    "FindingNotFound",
    "FindingSummary",
    "NoProviderConfigured",
    "ProviderUnavailable",
    "UnidentifiableFinding",
    "build_entity_string",
    "build_prompt",
    "build_techniques_string",
    "enrich",
    "extract_json_block",
    "parse_enrichment",
    "summarize_finding",
]
