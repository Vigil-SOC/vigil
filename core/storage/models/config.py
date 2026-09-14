"""System, integration, federation, and threat-intel ORM models."""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base
from core.time import utcnow


class SketchMapping(Base):
    """Timesketch mapping model - links cases/findings to Timesketch sketches."""

    __tablename__ = "sketch_mappings"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Mapping information
    case_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=True
    )
    finding_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("findings.finding_id", ondelete="CASCADE"), nullable=True
    )

    # Timesketch information
    sketch_id: Mapped[int] = mapped_column(Integer, nullable=False)
    sketch_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sketch_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )

    # Indexes
    __table_args__ = (
        Index("idx_sketch_case_id", "case_id"),
        Index("idx_sketch_finding_id", "finding_id"),
        Index("idx_sketch_id", "sketch_id"),
    )


class SystemConfig(Base):
    """
    System Configuration - Stores system-wide configuration settings.

    This replaces file-based configs in ~/.vigil/ for better multi-user
    support, ACID compliance, and audit trails.
    """

    __tablename__ = "system_config"

    # Primary key
    key: Mapped[str] = mapped_column(String(100), primary_key=True)

    # Configuration value (flexible JSONB storage)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Metadata
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="general", server_default="general"
    )

    # Audit fields
    updated_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_system_config_type", "config_type"),
        Index("idx_system_config_updated_at", "updated_at"),
    )


class UserPreference(Base):
    """
    User Preferences - Stores per-user preferences and settings.

    Supports multi-user deployments with individual user settings.
    """

    __tablename__ = "user_preferences"

    # Primary key
    user_id: Mapped[str] = mapped_column(String(100), primary_key=True)

    # Preferences as flexible JSONB
    preferences: Mapped[dict] = mapped_column(JSONB, nullable=False, default={})

    # User metadata
    display_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default="now()",
    )

    # Last login tracking
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class IntegrationConfig(Base):
    """
    Integration Configuration - Stores non-sensitive integration settings.

    Note: Secrets (API keys, passwords) remain in secrets_manager for security.
    This stores connection details, preferences, and enabled/disabled state.
    """

    __tablename__ = "integration_configs"

    # Primary key
    integration_id: Mapped[str] = mapped_column(String(100), primary_key=True)

    # Integration state
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Configuration (non-sensitive only)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default={})

    # Metadata
    integration_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    integration_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Health status
    last_test_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_test_success: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Audit
    updated_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_integration_enabled", "enabled"),
        Index("idx_integration_type", "integration_type"),
        Index("idx_integration_updated_at", "updated_at"),
    )


class FederationSource(Base):
    """
    Federation Source - Per-source state for the federated monitoring poller.

    One row per data source the daemon pulls from on a configurable cadence.
    Rows are auto-seeded on daemon boot from configured integrations (default
    disabled). The global on/off lives in ``system_config`` under the key
    ``federation.settings`` — a source only polls when both the global toggle
    and its own ``enabled`` flag are true.
    """

    __tablename__ = "federation_sources"

    # Primary key — matches integration ids (e.g. "splunk", "crowdstrike")
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Toggle + cadence
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300, server_default="300"
    )
    max_items: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )

    # Optional severity floor: only ingest findings >= this severity.
    # Nullable means "no filter".
    min_severity: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    # Adapter-defined cursor (e.g. {"earliest_time": "..."} for Splunk).
    cursor: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # Health
    last_poll_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

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

    __table_args__ = (Index("idx_federation_sources_enabled", "enabled"),)


class ConfigAuditLog(Base):
    """
    Configuration Audit Log - Tracks all configuration changes for compliance.

    Provides full audit trail of who changed what and when.
    """

    __tablename__ = "config_audit_log"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # What was changed
    config_type: Mapped[str] = mapped_column(String(50), nullable=False)
    config_key: Mapped[str] = mapped_column(String(200), nullable=False)

    # Change details
    action: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # create, update, delete
    old_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Who made the change
    changed_by: Mapped[str] = mapped_column(String(100), nullable=False)
    change_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # When
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )

    # Indexes
    __table_args__ = (
        Index("idx_audit_config_type", "config_type"),
        Index("idx_audit_config_key", "config_key"),
        Index("idx_audit_changed_by", "changed_by"),
        Index("idx_audit_timestamp", "timestamp"),
    )


class SharedIOC(Base):
    """Cross-investigation IOC index for deduplication and correlation."""

    __tablename__ = "shared_iocs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(
        String(60),
        ForeignKey("investigations.investigation_id", ondelete="CASCADE"),
        nullable=False,
    )
    ioc_type: Mapped[str] = mapped_column(String(30), nullable=False)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )

    __table_args__ = (
        Index("idx_shared_ioc_value", "value"),
        Index("idx_shared_ioc_type", "ioc_type"),
        Index("idx_shared_ioc_investigation", "investigation_id"),
    )


class ThreatIndicator(Base):
    """Global threat indicator from external feeds (Cloudforce One STIX/TAXII, etc.).

    Distinct from `CaseIOC` (case-scoped). Polled by `daemon/threat_feed_poller.py`
    and joined against finding IOCs during enrichment in `daemon/processor.py`.
    See `infra/database/init/14_threat_indicators.sql`.
    """

    __tablename__ = "threat_indicators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    indicator_type: Mapped[str] = mapped_column(String(32), nullable=False)
    indicator_value: Mapped[str] = mapped_column(String(2048), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    collection_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    threat_level: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    labels: Mapped[Optional[List[str]]] = mapped_column(ARRAY(Text), nullable=True)
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    raw_stix: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )

    __table_args__ = (
        Index("idx_threat_indicators_type_value", "indicator_type", "indicator_value"),
        Index("idx_threat_indicators_source", "source"),
        Index("idx_threat_indicators_last_seen", "last_seen"),
        Index("idx_threat_indicators_valid_until", "valid_until"),
    )
