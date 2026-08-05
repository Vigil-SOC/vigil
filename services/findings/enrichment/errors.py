"""Domain exceptions for finding enrichment.

Raised instead of ``HTTPException`` so the flow is callable from the daemon and
from ingestion, neither of which speaks HTTP. ``backend/api/findings.py`` maps
these to status codes.
"""


class EnrichmentError(Exception):
    """Base class for every finding-enrichment failure."""


class FindingNotFound(EnrichmentError):
    """The finding to enrich is empty."""


class UnidentifiableFinding(EnrichmentError):
    """No finding id available, so the result can't be persisted."""


class NoProviderConfigured(EnrichmentError):
    """No usable LLM provider for the requested component."""


class ProviderUnavailable(EnrichmentError):
    """The resolved provider id has no provider spec row."""


class EmptyProviderResponse(EnrichmentError):
    """The provider returned no content to parse."""
