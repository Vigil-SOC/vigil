"""Serialization schema for the Finding model."""

from typing import Any, Optional

from pydantic import model_validator

from core.storage.schemas.base import OptDateTime, ORMSchema


def _predictions_map(obj: Any) -> dict:
    """Rebuild `{technique_id: confidence}` from child rows."""
    rows = getattr(obj, "mitre_prediction_rows", None)
    if not rows:
        return {}
    return {row.technique_id: row.confidence for row in rows}


class FindingSchema(ORMSchema):
    """A security finding."""

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

    @model_validator(mode="wrap")
    @classmethod
    def _hydrate_predictions(cls, data: Any, handler):
        """Nested dumps (e.g. CaseWithFindings) never call ``dump``."""
        validated = handler(data)
        if getattr(data, "mitre_prediction_rows", None) is not None:
            validated.mitre_predictions = _predictions_map(data)
        return validated

    @classmethod
    def dump(cls, obj: Any, **kwargs: Any) -> dict:
        data = super().dump(obj, **kwargs)
        data["mitre_predictions"] = _predictions_map(obj)
        return data
