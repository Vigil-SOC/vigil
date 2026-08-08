"""Serialization schema for the Finding model."""

from typing import Any, Optional

from core.storage.schemas.base import Embedding, OptDateTime, ORMSchema

# List/summary responses never consume the embedding, and shipping a
# 768-float vector per row is expensive — omit the field entirely.
WITHOUT_EMBEDDING = {"embedding"}


class FindingSchema(ORMSchema):
    """A security finding.

    ``embedding`` is present by default; callers that don't need the vector
    should use :meth:`dump_summary`.
    """

    finding_id: Optional[str] = None
    description: Optional[str] = None
    mitre_predictions: Optional[Any] = None
    anomaly_score: Optional[float] = None
    entity_context: Optional[Any] = None
    evidence_links: Optional[Any] = None
    timestamp: OptDateTime = None
    data_source: Optional[str] = None
    external_id: Optional[str] = None
    cluster_id: Optional[str] = None
    severity: Optional[str] = None
    status: Optional[str] = None
    ai_enrichment: Optional[Any] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None
    embedding: Embedding = None

    @classmethod
    def dump_summary(cls, obj: Any, **kwargs: Any) -> dict:
        """Serialize without the embedding vector."""
        return cls.dump(obj, exclude=WITHOUT_EMBEDDING, **kwargs)

    @classmethod
    def dump_many_summary(cls, objs: Any, **kwargs: Any) -> list[dict]:
        """Serialize an iterable without embedding vectors."""
        return [cls.dump_summary(obj, **kwargs) for obj in objs]
