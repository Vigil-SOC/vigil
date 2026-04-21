"""Pydantic schemas for the VStrike (CloudCurrent) integration.

VStrike is a network-topology fusion layer that enriches DeepTempo LogLM
findings with operational context (asset, segment, site, mission system,
adjacent nodes, attack path, blast radius) and pushes the enriched findings
to Vigil via `POST /api/integrations/vstrike/findings`.

Enrichment is persisted inside `finding.entity_context["vstrike"]`.
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class VStrikeAdjacentAsset(BaseModel):
    """A neighbor of the primary asset in the network graph."""

    asset_id: str
    asset_name: Optional[str] = None
    segment: Optional[str] = None
    hop_distance: int = 1
    edge_technique: Optional[str] = Field(
        None,
        description="MITRE ATT&CK technique ID (e.g. T1021.002) if this edge "
        "represents an observed or inferred attack-path step.",
    )


class VStrikeEnrichment(BaseModel):
    """Network-topology context attached to a finding.

    Persisted at `finding.entity_context["vstrike"]`.
    """

    asset_id: str
    asset_name: Optional[str] = None
    segment: str
    site: Optional[str] = None
    criticality: Literal["low", "medium", "high", "critical"]
    mission_system: Optional[str] = None
    adjacent_assets: List[VStrikeAdjacentAsset] = Field(default_factory=list)
    attack_path: List[str] = Field(
        default_factory=list,
        description="Ordered list of asset_ids from initial access to this "
        "finding's asset. The last element is the asset tied to this finding.",
    )
    blast_radius: Optional[int] = Field(
        None, description="Count of assets reachable from this one."
    )
    topology_metadata: Optional[Dict[str, Any]] = Field(
        None,
        description="Escape hatch for VStrike-specific fields not yet modelled.",
    )
    enriched_at: datetime


class VStrikeFinding(BaseModel):
    """A single finding pushed from VStrike.

    `finding_id` must match a DeepTempo LogLM finding already known to Vigil
    (update path). If it does not exist and `timestamp` + `anomaly_score` are
    supplied, a new finding is created with `data_source="vstrike"`.
    """

    finding_id: str
    vstrike_enrichment: VStrikeEnrichment

    # Optional finding-creation fields (used only if finding_id is new)
    timestamp: Optional[datetime] = None
    anomaly_score: Optional[float] = None
    severity: Optional[Literal["low", "medium", "high", "critical"]] = None
    mitre_predictions: Optional[Dict[str, float]] = None
    predicted_techniques: Optional[List[Dict[str, Any]]] = None
    description: Optional[str] = None

    # Merged into entity_context alongside the vstrike sub-dict
    entity_context_extra: Optional[Dict[str, Any]] = None


class VStrikePushRequest(BaseModel):
    """Batch payload from VStrike.

    Example:
        {
            "batch_id": "vstrike-2026-05-20-1420",
            "findings": [...],
            "auto_cluster_cases": true
        }
    """

    batch_id: str
    source: str = "vstrike"
    findings: List[VStrikeFinding]
    auto_cluster_cases: bool = True


class VStrikeFindingResult(BaseModel):
    """Per-finding outcome in the push response."""

    finding_id: str
    status: Literal["updated", "created", "failed"]
    error: Optional[str] = None


class VStrikePushResponse(BaseModel):
    """Response for POST /api/integrations/vstrike/findings."""

    batch_id: str
    received: int
    updated: int
    created: int
    failed: int
    results: List[VStrikeFindingResult]
    case_ids: List[str] = Field(default_factory=list)


class VStrikeHealthResponse(BaseModel):
    """Response for GET /api/integrations/vstrike/health."""

    configured: bool
    reachable: bool
    base_url: Optional[str] = None
    message: str
