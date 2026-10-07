"""Finding ORM models."""

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)

from core.storage.models.base import Base, case_findings
from core.time import utcnow

if TYPE_CHECKING:
    from core.storage.models.case import Case


class Finding(Base):
    """Finding model - represents a security finding from DeepTempo LogLM."""

    __tablename__ = "findings"

    # Primary key
    finding_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    anomaly_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Human-readable description (populated from ingestion or synthesized from entity_context)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Entity context (optional fields)
    entity_context: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Evidence links
    evidence_links: Mapped[Optional[List[dict]]] = mapped_column(JSONB, nullable=True)

    # Metadata
    timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    data_source: Mapped[str] = mapped_column(String(50), nullable=False)
    # Source-native ID. Combined with data_source it forms the dedup key
    # for federated ingest (see uniq_findings_source_extid).
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    cluster_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    severity: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="new", server_default="new"
    )
    # A noise mark hides the row from the Overview feed. It is not a disposition
    # and does not change status or scoring.
    noise_marked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    noise_marked_by: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # AI-generated enrichment (cached analysis)
    ai_enrichment: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )

    # Relationships
    cases: Mapped[List["Case"]] = relationship(
        "Case", secondary=case_findings, back_populates="findings", lazy="selectin"
    )
    mitre_prediction_rows: Mapped[List["FindingMitrePrediction"]] = relationship(
        "FindingMitrePrediction",
        back_populates="finding",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    # Indexes
    __table_args__ = (
        Index("idx_finding_timestamp", "timestamp"),
        Index("idx_finding_severity", "severity"),
        Index("idx_finding_status", "status"),
        Index("idx_finding_data_source", "data_source"),
        Index("idx_finding_cluster_id", "cluster_id"),
        Index("idx_finding_anomaly_score", "anomaly_score"),
        Index(
            "idx_finding_description",
            "description",
            postgresql_ops={"description": "gin_trgm_ops"},
            postgresql_using="gin",
        ),
        Index(
            "uniq_findings_source_extid",
            "data_source",
            "external_id",
            unique=True,
            postgresql_where=text(
                "data_source IS NOT NULL AND external_id IS NOT NULL"
            ),
        ),
    )


class FindingMitrePrediction(Base):
    """One predicted ATT&CK technique (or ingest key) for a finding.

    Keys are stored as text: parquet/CSV ingest uses tactic names, not only T-IDs.
    """

    __tablename__ = "finding_mitre_predictions"

    finding_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("findings.finding_id", ondelete="CASCADE"),
        primary_key=True,
    )
    technique_id: Mapped[str] = mapped_column(Text, primary_key=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    finding: Mapped["Finding"] = relationship(
        "Finding", back_populates="mitre_prediction_rows"
    )

    __table_args__ = (
        Index(
            "idx_finding_mitre_predictions_technique_confidence",
            "technique_id",
            text("confidence DESC"),
        ),
    )
