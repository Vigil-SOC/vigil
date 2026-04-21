"""
SQLAlchemy Database Models for Vigil SOC

Defines the database schema for cases, findings, and related entities.
"""

from datetime import datetime
from typing import Optional, List
from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    DateTime,
    Text,
    JSON,
    ForeignKey,
    Table,
    Index,
    Boolean,
    ARRAY,
    Numeric,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import JSONB, UUID
import uuid


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


# Association table for case-finding many-to-many relationship
case_findings = Table(
    "case_findings",
    Base.metadata,
    Column(
        "case_id",
        String,
        ForeignKey("cases.case_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "finding_id",
        String,
        ForeignKey("findings.finding_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("added_at", DateTime, default=datetime.utcnow, nullable=False),
)


class Finding(Base):
    """Finding model - represents a security finding from DeepTempo LogLM."""

    __tablename__ = "findings"

    # Primary key
    finding_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Core finding data
    embedding: Mapped[List[float]] = mapped_column(ARRAY(Float), nullable=False)
    mitre_predictions: Mapped[dict] = mapped_column(JSONB, nullable=False)
    anomaly_score: Mapped[float] = mapped_column(Float, nullable=False)

    # Human-readable description (populated from ingestion or synthesized from entity_context)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Entity context (optional fields)
    entity_context: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Evidence links
    evidence_links: Mapped[Optional[List[dict]]] = mapped_column(JSONB, nullable=True)

    # Metadata
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    data_source: Mapped[str] = mapped_column(String(50), nullable=False)
    cluster_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    severity: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="new", server_default="new"
    )

    # AI-generated enrichment (cached analysis)
    ai_enrichment: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Relationships
    cases: Mapped[List["Case"]] = relationship(
        "Case", secondary=case_findings, back_populates="findings", lazy="selectin"
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
    )

    def to_dict(self) -> dict:
        """Convert finding to dictionary."""
        return {
            "finding_id": self.finding_id,
            "embedding": self.embedding,
            "description": self.description,
            "mitre_predictions": self.mitre_predictions,
            "anomaly_score": self.anomaly_score,
            "entity_context": self.entity_context,
            "evidence_links": self.evidence_links,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "data_source": self.data_source,
            "cluster_id": self.cluster_id,
            "severity": self.severity,
            "status": self.status,
            "ai_enrichment": self.ai_enrichment,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Case(Base):
    """Case model - represents an investigation case grouping related findings."""

    __tablename__ = "cases"

    # Primary key
    case_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Basic case information
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, default="")

    # Status and priority
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="new", server_default="new"
    )
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium", server_default="medium"
    )

    # Assignment
    assignee: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Tags (array of strings)
    tags: Mapped[List[str]] = mapped_column(ARRAY(String), nullable=True, default=[])

    # Notes (JSONB array)
    notes: Mapped[List[dict]] = mapped_column(JSONB, nullable=True, default=[])

    # Timeline events (JSONB array)
    timeline: Mapped[List[dict]] = mapped_column(JSONB, nullable=False, default=[])

    # Activities (JSONB array)
    activities: Mapped[Optional[List[dict]]] = mapped_column(
        JSONB, nullable=True, default=[]
    )

    # Resolution steps (JSONB array)
    resolution_steps: Mapped[Optional[List[dict]]] = mapped_column(
        JSONB, nullable=True, default=[]
    )

    # MITRE ATT&CK techniques
    mitre_techniques: Mapped[Optional[List[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Relationships
    findings: Mapped[List[Finding]] = relationship(
        "Finding", secondary=case_findings, back_populates="cases", lazy="selectin"
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_status", "status"),
        Index("idx_case_priority", "priority"),
        Index("idx_case_assignee", "assignee"),
        Index("idx_case_created_at", "created_at"),
        Index("idx_case_updated_at", "updated_at"),
    )

    def to_dict(self, include_findings: bool = False) -> dict:
        """Convert case to dictionary."""
        result = {
            "case_id": self.case_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "assignee": self.assignee,
            "tags": self.tags or [],
            "notes": self.notes or [],
            "timeline": self.timeline or [],
            "activities": self.activities or [],
            "resolution_steps": self.resolution_steps or [],
            "mitre_techniques": self.mitre_techniques or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        if include_findings:
            result["findings"] = [f.to_dict() for f in self.findings]
        else:
            result["finding_ids"] = [f.finding_id for f in self.findings]

        return result


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
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )

    # Indexes
    __table_args__ = (
        Index("idx_sketch_case_id", "case_id"),
        Index("idx_sketch_finding_id", "finding_id"),
        Index("idx_sketch_id", "sketch_id"),
    )

    def to_dict(self) -> dict:
        """Convert sketch mapping to dictionary."""
        return {
            "id": self.id,
            "case_id": self.case_id,
            "finding_id": self.finding_id,
            "sketch_id": self.sketch_id,
            "sketch_name": self.sketch_name,
            "sketch_url": self.sketch_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AttackLayer(Base):
    """ATT&CK Navigator layer storage."""

    __tablename__ = "attack_layers"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Layer information
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    layer_data: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Association with case (optional)
    case_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="SET NULL"), nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_attack_layer_case_id", "case_id"),
        Index("idx_attack_layer_created_at", "created_at"),
    )

    def to_dict(self) -> dict:
        """Convert attack layer to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "layer_data": self.layer_data,
            "case_id": self.case_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AIDecisionLog(Base):
    """
    AI Decision Log - Tracks AI decisions for feedback and learning.

    This model enables human oversight and continuous improvement of AI agents
    by tracking all AI decisions, collecting human feedback, and measuring accuracy.
    """

    __tablename__ = "ai_decision_logs"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)

    # Decision context
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    workflow_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    finding_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("findings.finding_id", ondelete="CASCADE"), nullable=True
    )
    case_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=True
    )

    # AI's decision
    decision_type: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)

    # Additional decision metadata
    decision_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Human feedback
    human_reviewer: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    human_decision: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    feedback_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Grading (0-1 scale)
    accuracy_grade: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reasoning_grade: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    action_appropriateness: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    # Outcome tracking
    actual_outcome: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    time_saved_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Timestamps
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    feedback_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # Indexes
    __table_args__ = (
        Index("idx_ai_decision_agent_id", "agent_id"),
        Index("idx_ai_decision_finding_id", "finding_id"),
        Index("idx_ai_decision_case_id", "case_id"),
        Index("idx_ai_decision_timestamp", "timestamp"),
        Index("idx_ai_decision_human_decision", "human_decision"),
        Index("idx_ai_decision_actual_outcome", "actual_outcome"),
    )

    def to_dict(self) -> dict:
        """Convert AI decision log to dictionary."""
        return {
            "id": self.id,
            "decision_id": self.decision_id,
            "agent_id": self.agent_id,
            "workflow_id": self.workflow_id,
            "finding_id": self.finding_id,
            "case_id": self.case_id,
            "decision_type": self.decision_type,
            "confidence_score": self.confidence_score,
            "reasoning": self.reasoning,
            "recommended_action": self.recommended_action,
            "decision_metadata": self.decision_metadata,
            "human_reviewer": self.human_reviewer,
            "human_decision": self.human_decision,
            "feedback_comment": self.feedback_comment,
            "accuracy_grade": self.accuracy_grade,
            "reasoning_grade": self.reasoning_grade,
            "action_appropriateness": self.action_appropriateness,
            "actual_outcome": self.actual_outcome,
            "time_saved_minutes": self.time_saved_minutes,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "feedback_timestamp": (
                self.feedback_timestamp.isoformat() if self.feedback_timestamp else None
            ),
        }


class SystemConfig(Base):
    """
    System Configuration - Stores system-wide configuration settings.

    This replaces file-based configs in ~/.deeptempo/ for better multi-user
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
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_system_config_type", "config_type"),
        Index("idx_system_config_updated_at", "updated_at"),
    )

    def to_dict(self) -> dict:
        """Convert system config to dictionary."""
        return {
            "key": self.key,
            "value": self.value,
            "description": self.description,
            "config_type": self.config_type,
            "updated_by": self.updated_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


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
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Last login tracking
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    def to_dict(self) -> dict:
        """Convert user preference to dictionary."""
        return {
            "user_id": self.user_id,
            "preferences": self.preferences,
            "display_name": self.display_name,
            "email": self.email,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_login": self.last_login.isoformat() if self.last_login else None,
        }


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
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_integration_enabled", "enabled"),
        Index("idx_integration_type", "integration_type"),
        Index("idx_integration_updated_at", "updated_at"),
    )

    def to_dict(self) -> dict:
        """Convert integration config to dictionary."""
        return {
            "integration_id": self.integration_id,
            "enabled": self.enabled,
            "config": self.config,
            "integration_name": self.integration_name,
            "integration_type": self.integration_type,
            "description": self.description,
            "last_test_at": (
                self.last_test_at.isoformat() if self.last_test_at else None
            ),
            "last_test_success": self.last_test_success,
            "last_error": self.last_error,
            "updated_by": self.updated_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


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
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )

    # Indexes
    __table_args__ = (
        Index("idx_audit_config_type", "config_type"),
        Index("idx_audit_config_key", "config_key"),
        Index("idx_audit_changed_by", "changed_by"),
        Index("idx_audit_timestamp", "timestamp"),
    )

    def to_dict(self) -> dict:
        """Convert audit log entry to dictionary."""
        return {
            "id": self.id,
            "config_type": self.config_type,
            "config_key": self.config_key,
            "action": self.action,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "changed_by": self.changed_by,
            "change_reason": self.change_reason,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


# =============================================================================
# Enhanced Case Management Models
# =============================================================================


class SLAPolicy(Base):
    """
    SLA Policy - Configurable service level agreement policies.

    Defines response and resolution time requirements based on case priority.
    """

    __tablename__ = "sla_policies"

    # Primary key
    policy_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Policy details
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    priority_level: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # critical, high, medium, low

    # Time requirements (in hours)
    response_time_hours: Mapped[float] = mapped_column(Float, nullable=False)
    resolution_time_hours: Mapped[float] = mapped_column(Float, nullable=False)

    # Business hours settings
    business_hours_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    # Escalation rules (JSONB)
    escalation_rules: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Notification thresholds (e.g., [75, 90, 100] for 75%, 90%, 100% of time elapsed)
    notification_thresholds: Mapped[Optional[List[int]]] = mapped_column(
        ARRAY(Integer), nullable=True
    )

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_sla_policy_priority", "priority_level"),
        Index("idx_sla_policy_active", "is_active"),
        Index("idx_sla_policy_default", "is_default"),
    )

    def to_dict(self) -> dict:
        """Convert SLA policy to dictionary."""
        return {
            "policy_id": self.policy_id,
            "name": self.name,
            "description": self.description,
            "priority_level": self.priority_level,
            "response_time_hours": self.response_time_hours,
            "resolution_time_hours": self.resolution_time_hours,
            "business_hours_only": self.business_hours_only,
            "escalation_rules": self.escalation_rules,
            "notification_thresholds": self.notification_thresholds,
            "is_active": self.is_active,
            "is_default": self.is_default,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CaseSLA(Base):
    """
    Case SLA - Tracks SLA compliance for individual cases.

    Links cases to SLA policies and tracks deadlines, pauses, and breaches.
    """

    __tablename__ = "case_slas"

    # Primary key
    sla_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # References
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False
    )
    sla_policy_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("sla_policies.policy_id"), nullable=False
    )

    # Deadlines
    response_due: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolution_due: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Response tracking
    response_completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    response_sla_met: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Resolution tracking
    resolution_completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    resolution_sla_met: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Breach information
    breached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    breach_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    breach_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Pause tracking
    is_paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    total_pause_duration: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # in seconds

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_sla_case_id", "case_id"),
        Index("idx_case_sla_policy_id", "sla_policy_id"),
        Index("idx_case_sla_response_due", "response_due"),
        Index("idx_case_sla_resolution_due", "resolution_due"),
        Index("idx_case_sla_breached", "breached"),
    )

    def to_dict(self) -> dict:
        """Convert case SLA to dictionary."""
        return {
            "sla_id": self.sla_id,
            "case_id": self.case_id,
            "sla_policy_id": self.sla_policy_id,
            "response_due": (
                self.response_due.isoformat() if self.response_due else None
            ),
            "resolution_due": (
                self.resolution_due.isoformat() if self.resolution_due else None
            ),
            "response_completed_at": (
                self.response_completed_at.isoformat()
                if self.response_completed_at
                else None
            ),
            "response_sla_met": self.response_sla_met,
            "resolution_completed_at": (
                self.resolution_completed_at.isoformat()
                if self.resolution_completed_at
                else None
            ),
            "resolution_sla_met": self.resolution_sla_met,
            "breached": self.breached,
            "breach_time": self.breach_time.isoformat() if self.breach_time else None,
            "breach_reason": self.breach_reason,
            "is_paused": self.is_paused,
            "paused_at": self.paused_at.isoformat() if self.paused_at else None,
            "resumed_at": self.resumed_at.isoformat() if self.resumed_at else None,
            "total_pause_duration": self.total_pause_duration,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CaseComment(Base):
    """
    Case Comment - Discussion threads for cases.

    Supports threaded conversations, @mentions, and rich text.
    """

    __tablename__ = "case_comments"

    # Primary key
    comment_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # References
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False
    )
    parent_comment_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("case_comments.comment_id", ondelete="CASCADE"),
        nullable=True,
    )

    # Comment content
    author: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Mentions (user IDs)
    mentions: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)

    # Attachments (references to attachment IDs)
    attachment_ids: Mapped[Optional[List[int]]] = mapped_column(
        ARRAY(Integer), nullable=True
    )

    # Metadata
    is_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_comment_case_id", "case_id"),
        Index("idx_case_comment_author", "author"),
        Index("idx_case_comment_parent_id", "parent_comment_id"),
        Index("idx_case_comment_created_at", "created_at"),
    )

    def to_dict(self) -> dict:
        """Convert comment to dictionary."""
        return {
            "comment_id": self.comment_id,
            "case_id": self.case_id,
            "parent_comment_id": self.parent_comment_id,
            "author": self.author,
            "content": self.content,
            "mentions": self.mentions or [],
            "attachment_ids": self.attachment_ids or [],
            "is_edited": self.is_edited,
            "is_deleted": self.is_deleted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CaseWatcher(Base):
    """
    Case Watcher - Tracks users who are watching/following cases.

    Enables notification subscriptions for case updates.
    """

    __tablename__ = "case_watchers"

    # Composite primary key
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(String(100), primary_key=True)

    # Notification preferences (JSONB)
    notification_preferences: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_watcher_case_id", "case_id"),
        Index("idx_case_watcher_user_id", "user_id"),
    )

    def to_dict(self) -> dict:
        """Convert watcher to dictionary."""
        return {
            "case_id": self.case_id,
            "user_id": self.user_id,
            "notification_preferences": self.notification_preferences,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CaseEvidence(Base):
    """
    Case Evidence - Tracks evidence and artifacts for cases.

    Maintains chain of custody and evidence metadata.
    """

    __tablename__ = "case_evidence"

    # Primary key
    evidence_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # References
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False
    )

    # Evidence details
    evidence_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # file, log, network_capture, memory_dump, etc.
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # File information
    file_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_hash_md5: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    file_hash_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Source information
    source: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    collected_by: Mapped[str] = mapped_column(String(100), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Chain of custody (JSONB array of custody entries)
    chain_of_custody: Mapped[List[dict]] = mapped_column(
        JSONB, nullable=False, default=[]
    )

    # Analysis results
    analysis_results: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Tags
    tags: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_evidence_case_id", "case_id"),
        Index("idx_case_evidence_type", "evidence_type"),
        Index("idx_case_evidence_collected_by", "collected_by"),
        Index("idx_case_evidence_collected_at", "collected_at"),
    )

    def to_dict(self) -> dict:
        """Convert evidence to dictionary."""
        return {
            "evidence_id": self.evidence_id,
            "case_id": self.case_id,
            "evidence_type": self.evidence_type,
            "name": self.name,
            "description": self.description,
            "file_path": self.file_path,
            "file_size": self.file_size,
            "file_hash_md5": self.file_hash_md5,
            "file_hash_sha256": self.file_hash_sha256,
            "source": self.source,
            "collected_by": self.collected_by,
            "collected_at": (
                self.collected_at.isoformat() if self.collected_at else None
            ),
            "chain_of_custody": self.chain_of_custody or [],
            "analysis_results": self.analysis_results,
            "tags": self.tags or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CaseIOC(Base):
    """
    Case IOC - Indicators of Compromise associated with cases.

    Tracks malicious indicators (IPs, domains, hashes, etc.).
    """

    __tablename__ = "case_iocs"

    # Primary key
    ioc_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # References
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False
    )

    # IOC details
    ioc_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # ip, domain, hash, url, email, file_name, etc.
    value: Mapped[str] = mapped_column(String(500), nullable=False)

    # Threat information
    threat_level: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )  # critical, high, medium, low
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Source information
    source: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    first_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Enrichment data from threat intel sources
    enrichment_data: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    reputation_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Tags and context
    tags: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)
    context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_false_positive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_ioc_case_id", "case_id"),
        Index("idx_case_ioc_type", "ioc_type"),
        Index("idx_case_ioc_value", "value"),
        Index("idx_case_ioc_threat_level", "threat_level"),
        Index("idx_case_ioc_is_active", "is_active"),
    )

    def to_dict(self) -> dict:
        """Convert IOC to dictionary."""
        return {
            "ioc_id": self.ioc_id,
            "case_id": self.case_id,
            "ioc_type": self.ioc_type,
            "value": self.value,
            "threat_level": self.threat_level,
            "confidence": self.confidence,
            "source": self.source,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "enrichment_data": self.enrichment_data,
            "reputation_score": self.reputation_score,
            "tags": self.tags or [],
            "context": self.context,
            "is_active": self.is_active,
            "is_false_positive": self.is_false_positive,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CaseTask(Base):
    """
    Case Task - Tasks and sub-tasks for case investigations.

    Supports hierarchical task structure and checklists.
    """

    __tablename__ = "case_tasks"

    # Primary key
    task_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # References
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False
    )
    parent_task_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("case_tasks.task_id", ondelete="CASCADE"), nullable=True
    )

    # Task details
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Assignment and status
    assignee: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending, in_progress, completed, cancelled
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")

    # Time tracking
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    estimated_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    actual_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Checklist items (JSONB array)
    checklist_items: Mapped[Optional[List[dict]]] = mapped_column(JSONB, nullable=True)

    # Metadata
    task_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_task_case_id", "case_id"),
        Index("idx_case_task_parent_id", "parent_task_id"),
        Index("idx_case_task_assignee", "assignee"),
        Index("idx_case_task_status", "status"),
        Index("idx_case_task_due_date", "due_date"),
    )

    def to_dict(self) -> dict:
        """Convert task to dictionary."""
        return {
            "task_id": self.task_id,
            "case_id": self.case_id,
            "parent_task_id": self.parent_task_id,
            "title": self.title,
            "description": self.description,
            "assignee": self.assignee,
            "status": self.status,
            "priority": self.priority,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "estimated_hours": self.estimated_hours,
            "actual_hours": self.actual_hours,
            "checklist_items": self.checklist_items or [],
            "task_order": self.task_order,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CaseTemplate(Base):
    """
    Case Template - Reusable templates for common investigation types.

    Includes pre-defined tasks, playbooks, and default settings.
    """

    __tablename__ = "case_templates"

    # Primary key
    template_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Template details
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    template_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # malware, phishing, data_exfiltration, etc.

    # Default case settings
    default_priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium"
    )
    default_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open"
    )
    default_sla_policy_id: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )

    # Task templates (JSONB array)
    task_templates: Mapped[List[dict]] = mapped_column(
        JSONB, nullable=False, default=[]
    )

    # Playbook steps (JSONB array)
    playbook_steps: Mapped[Optional[List[dict]]] = mapped_column(JSONB, nullable=True)

    # MITRE ATT&CK techniques
    applicable_mitre_techniques: Mapped[Optional[List[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )

    # Template metadata
    tags: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_template_type", "template_type"),
        Index("idx_case_template_active", "is_active"),
        Index("idx_case_template_usage_count", "usage_count"),
    )

    def to_dict(self) -> dict:
        """Convert template to dictionary."""
        return {
            "template_id": self.template_id,
            "name": self.name,
            "description": self.description,
            "template_type": self.template_type,
            "default_priority": self.default_priority,
            "default_status": self.default_status,
            "default_sla_policy_id": self.default_sla_policy_id,
            "task_templates": self.task_templates or [],
            "playbook_steps": self.playbook_steps or [],
            "applicable_mitre_techniques": self.applicable_mitre_techniques or [],
            "tags": self.tags or [],
            "is_active": self.is_active,
            "usage_count": self.usage_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CaseRelationship(Base):
    """
    Case Relationship - Links related cases together.

    Supports various relationship types (duplicate, related, parent-child, etc.).
    """

    __tablename__ = "case_relationships"

    # Primary key
    relationship_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # References
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False
    )
    related_case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False
    )

    # Relationship type
    relationship_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # duplicate, related, parent, child, blocks, blocked_by

    # Metadata
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_relationship_case_id", "case_id"),
        Index("idx_case_relationship_related_case_id", "related_case_id"),
        Index("idx_case_relationship_type", "relationship_type"),
    )

    def to_dict(self) -> dict:
        """Convert relationship to dictionary."""
        return {
            "relationship_id": self.relationship_id,
            "case_id": self.case_id,
            "related_case_id": self.related_case_id,
            "relationship_type": self.relationship_type,
            "created_by": self.created_by,
            "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CaseMetrics(Base):
    """
    Case Metrics - Performance and time tracking metrics for cases.

    Tracks key metrics like MTTD, MTTR, MTTA, etc.
    """

    __tablename__ = "case_metrics"

    # Primary key (one-to-one with cases)
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), primary_key=True
    )

    # Time metrics (in seconds)
    time_to_detect: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_to_respond: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_to_contain: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    time_to_resolve: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Work tracking
    total_work_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    analyst_handoffs_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # SLA tracking
    sla_met: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    response_sla_met: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    resolution_sla_met: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Activity metrics
    comment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ioc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    def to_dict(self) -> dict:
        """Convert metrics to dictionary."""
        return {
            "case_id": self.case_id,
            "time_to_detect": self.time_to_detect,
            "time_to_respond": self.time_to_respond,
            "time_to_contain": self.time_to_contain,
            "time_to_resolve": self.time_to_resolve,
            "total_work_hours": self.total_work_hours,
            "analyst_handoffs_count": self.analyst_handoffs_count,
            "sla_met": self.sla_met,
            "response_sla_met": self.response_sla_met,
            "resolution_sla_met": self.resolution_sla_met,
            "comment_count": self.comment_count,
            "evidence_count": self.evidence_count,
            "ioc_count": self.ioc_count,
            "task_count": self.task_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class CaseAttachment(Base):
    """
    Case Attachment - File attachments for cases.

    Stores metadata for files uploaded to cases.
    """

    __tablename__ = "case_attachments"

    # Primary key
    attachment_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # References
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False
    )

    # File details
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Metadata
    uploaded_by: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tags: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)

    # Security scan results
    virus_scan_result: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )  # clean, infected, suspicious, not_scanned
    scan_details: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_attachment_case_id", "case_id"),
        Index("idx_case_attachment_uploaded_by", "uploaded_by"),
        Index("idx_case_attachment_created_at", "created_at"),
    )

    def to_dict(self) -> dict:
        """Convert attachment to dictionary."""
        return {
            "attachment_id": self.attachment_id,
            "case_id": self.case_id,
            "filename": self.filename,
            "file_path": self.file_path,
            "file_type": self.file_type,
            "file_size": self.file_size,
            "mime_type": self.mime_type,
            "uploaded_by": self.uploaded_by,
            "description": self.description,
            "tags": self.tags or [],
            "virus_scan_result": self.virus_scan_result,
            "scan_details": self.scan_details,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CaseClosureInfo(Base):
    """
    Case Closure Info - Detailed closure metadata for closed cases.

    Captures root cause, lessons learned, and post-incident information.
    """

    __tablename__ = "case_closure_info"

    # Primary key (one-to-one with cases)
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), primary_key=True
    )

    # Closure details
    closure_category: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # resolved, false_positive, duplicate, unable_to_resolve, etc.

    # Root cause analysis
    root_cause: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    contributing_factors: Mapped[Optional[List[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )

    # Post-incident review
    lessons_learned: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recommendations: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recurrence_prevention: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # False positive details
    false_positive_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Summary
    executive_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Closure metadata
    closed_by: Mapped[str] = mapped_column(String(100), nullable=False)
    closure_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    closed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )

    def to_dict(self) -> dict:
        """Convert closure info to dictionary."""
        return {
            "case_id": self.case_id,
            "closure_category": self.closure_category,
            "root_cause": self.root_cause,
            "contributing_factors": self.contributing_factors or [],
            "lessons_learned": self.lessons_learned,
            "recommendations": self.recommendations,
            "recurrence_prevention": self.recurrence_prevention,
            "false_positive_reason": self.false_positive_reason,
            "executive_summary": self.executive_summary,
            "closed_by": self.closed_by,
            "closure_notes": self.closure_notes,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


class CaseEscalation(Base):
    """
    Case Escalation - Tracks escalations for cases.

    Records when and why cases are escalated to higher tiers or management.
    """

    __tablename__ = "case_escalations"

    # Primary key
    escalation_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # References
    case_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=False
    )

    # Escalation details
    escalated_from: Mapped[str] = mapped_column(String(100), nullable=False)
    escalated_to: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    urgency_level: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # low, medium, high, critical

    # Status tracking
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending, acknowledged, resolved

    # Timestamps
    escalated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Resolution
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_case_escalation_case_id", "case_id"),
        Index("idx_case_escalation_escalated_to", "escalated_to"),
        Index("idx_case_escalation_status", "status"),
        Index("idx_case_escalation_escalated_at", "escalated_at"),
    )

    def to_dict(self) -> dict:
        """Convert escalation to dictionary."""
        return {
            "escalation_id": self.escalation_id,
            "case_id": self.case_id,
            "escalated_from": self.escalated_from,
            "escalated_to": self.escalated_to,
            "reason": self.reason,
            "urgency_level": self.urgency_level,
            "status": self.status,
            "escalated_at": (
                self.escalated_at.isoformat() if self.escalated_at else None
            ),
            "acknowledged_at": (
                self.acknowledged_at.isoformat() if self.acknowledged_at else None
            ),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolution_notes": self.resolution_notes,
        }


class CaseAuditLog(Base):
    """
    Case Audit Log - Field-level audit trail for case changes.

    Tracks all modifications to cases and related entities for compliance.
    """

    __tablename__ = "case_audit_logs"

    # Primary key
    audit_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # What was changed
    entity_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # case, comment, evidence, ioc, etc.
    entity_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # Change details
    action: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # create, update, delete
    field_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    old_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Additional context
    change_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Who made the change
    changed_by: Mapped[str] = mapped_column(String(100), nullable=False)
    user_agent: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    # Timestamp
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_audit_entity_type", "entity_type"),
        Index("idx_case_audit_entity_id", "entity_id"),
        Index("idx_case_audit_changed_by", "changed_by"),
        Index("idx_case_audit_timestamp", "timestamp"),
        Index("idx_case_audit_action", "action"),
    )

    def to_dict(self) -> dict:
        """Convert audit log to dictionary."""
        return {
            "audit_id": self.audit_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "action": self.action,
            "field_name": self.field_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "change_summary": self.change_summary,
            "changed_by": self.changed_by,
            "user_agent": self.user_agent,
            "ip_address": self.ip_address,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


class User(Base):
    """
    User Model - System users with authentication and authorization.

    Stores user credentials, profile information, and role assignments.
    """

    __tablename__ = "users"

    # Primary key
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Authentication
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Profile
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Role and permissions
    role_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("roles.role_id"), nullable=False
    )

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # MFA
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mfa_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Session tracking
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Failed-login tracking and account lockout
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default='0'
    )
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Password history — list of prior bcrypt hashes, newest first. Used
    # to reject reuse of the last N passwords. Capped in application code.
    password_history: Mapped[List[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default='[]'
    )
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_user_username", "username"),
        Index("idx_user_email", "email"),
        Index("idx_user_role_id", "role_id"),
        Index("idx_user_is_active", "is_active"),
    )

    def to_dict(self) -> dict:
        """Convert user to dictionary (excluding password)."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "full_name": self.full_name,
            "role_id": self.role_id,
            "is_active": self.is_active,
            "is_verified": self.is_verified,
            "mfa_enabled": self.mfa_enabled,
            "last_login": self.last_login.isoformat() if self.last_login else None,
            "login_count": self.login_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Role(Base):
    """
    Role Model - Defines user roles and their permissions.

    RBAC (Role-Based Access Control) system for authorization.
    """

    __tablename__ = "roles"

    # Primary key
    role_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Role details
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Permissions (JSONB for flexibility)
    permissions: Mapped[dict] = mapped_column(JSONB, nullable=False, default={})

    # System role flag (cannot be deleted/modified)
    is_system_role: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (Index("idx_role_name", "name"),)

    def to_dict(self) -> dict:
        """Convert role to dictionary."""
        return {
            "role_id": self.role_id,
            "name": self.name,
            "description": self.description,
            "permissions": self.permissions,
            "is_system_role": self.is_system_role,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# =============================================================================
# Autonomous Orchestrator Models
# =============================================================================


class Investigation(Base):
    """Tracks an autonomous investigation assignment managed by the orchestrator."""

    __tablename__ = "investigations"

    investigation_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    case_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="SET NULL"), nullable=True
    )
    workflow_id: Mapped[str] = mapped_column(String(50), nullable=False)

    trigger_type: Mapped[str] = mapped_column(String(30), nullable=False)
    trigger_ids: Mapped[List[dict]] = mapped_column(JSONB, nullable=False, default=[])

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")

    workdir: Mapped[str] = mapped_column(String(255), nullable=False)

    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=50)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)

    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    max_runtime_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3600
    )

    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    proposed_actions: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    master_review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_activity: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_investigation_status", "status"),
        Index("idx_investigation_case_id", "case_id"),
        Index("idx_investigation_priority", "priority"),
        Index("idx_investigation_created_at", "created_at"),
        Index("idx_investigation_workflow_id", "workflow_id"),
    )

    def to_dict(self) -> dict:
        return {
            "investigation_id": self.investigation_id,
            "case_id": self.case_id,
            "workflow_id": self.workflow_id,
            "trigger_type": self.trigger_type,
            "trigger_ids": self.trigger_ids,
            "status": self.status,
            "workdir": self.workdir,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "iteration_count": self.iteration_count,
            "max_iterations": self.max_iterations,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "max_cost_usd": self.max_cost_usd,
            "priority": self.priority,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "last_activity_at": (
                self.last_activity_at.isoformat() if self.last_activity_at else None
            ),
            "max_runtime_seconds": self.max_runtime_seconds,
            "summary": self.summary,
            "proposed_actions": self.proposed_actions,
            "master_review_notes": self.master_review_notes,
            "error_count": self.error_count,
            "last_error": self.last_error,
            "current_activity": self.current_activity,
        }


class InvestigationLog(Base):
    """Append-only audit log for investigation agent actions."""

    __tablename__ = "investigation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(
        String(60),
        ForeignKey("investigations.investigation_id", ondelete="CASCADE"),
        nullable=False,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default={})
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_inv_log_investigation_id", "investigation_id"),
        Index("idx_inv_log_timestamp", "timestamp"),
        Index("idx_inv_log_event_type", "event_type"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "investigation_id": self.investigation_id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "event_type": self.event_type,
            "details": self.details,
            "tokens_used": self.tokens_used,
        }


class LLMInteractionLog(Base):
    """Durable audit log of Claude API interactions for chain-of-thought visibility.

    One row per Anthropic API response (per tool-use iteration). Captures
    full untruncated thinking blocks, tool call chains, token usage, and
    cost so analysts can audit AI decision-making post-hoc.
    """

    __tablename__ = "llm_interaction_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interaction_id: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    agent_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    investigation_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    request_messages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thinking_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    thinking_budget: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    thinking_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    tool_results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    stop_reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_llm_interaction_session", "session_id"),
        Index("idx_llm_interaction_agent", "agent_id"),
        Index("idx_llm_interaction_investigation", "investigation_id"),
        Index("idx_llm_interaction_created", "created_at"),
    )

    def to_summary_dict(self) -> dict:
        """Lightweight dict (no heavy text fields) for list endpoints."""
        return {
            "id": self.id,
            "interaction_id": self.interaction_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "investigation_id": self.investigation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "model": self.model,
            "thinking_enabled": self.thinking_enabled,
            "has_thinking": bool(self.thinking_content),
            "has_tools": bool(self.tool_calls),
            "stop_reason": self.stop_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": float(self.cost_usd) if self.cost_usd is not None else 0.0,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }

    def to_dict(self) -> dict:
        """Full dict including heavy text/JSONB fields for detail endpoint."""
        return {
            **self.to_summary_dict(),
            "request_messages": self.request_messages,
            "system_prompt": self.system_prompt,
            "thinking_budget": self.thinking_budget,
            "thinking_content": self.thinking_content,
            "response_content": self.response_content,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }


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
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )

    __table_args__ = (
        Index("idx_shared_ioc_value", "value"),
        Index("idx_shared_ioc_type", "ioc_type"),
        Index("idx_shared_ioc_investigation", "investigation_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "investigation_id": self.investigation_id,
            "ioc_type": self.ioc_type,
            "value": self.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CaseNotification(Base):
    """
    Case Notification - Notification queue for case-related events.

    Tracks notifications to be delivered to users about case updates.
    """

    __tablename__ = "case_notifications"

    # Primary key
    notification_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )

    # References
    case_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # Notification details
    notification_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # case_assigned, comment_mention, sla_warning, escalation, etc.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Delivery settings
    delivery_channel: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ui"
    )  # ui, email, slack, teams, pagerduty
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")

    # Status
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Metadata (renamed from 'metadata' to avoid SQLAlchemy reserved word conflict)
    notification_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default="now()"
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_notification_case_id", "case_id"),
        Index("idx_case_notification_user_id", "user_id"),
        Index("idx_case_notification_type", "notification_type"),
        Index("idx_case_notification_is_read", "is_read"),
        Index("idx_case_notification_is_sent", "is_sent"),
        Index("idx_case_notification_created_at", "created_at"),
    )

    def to_dict(self) -> dict:
        """Convert notification to dictionary."""
        return {
            "notification_id": self.notification_id,
            "case_id": self.case_id,
            "user_id": self.user_id,
            "notification_type": self.notification_type,
            "title": self.title,
            "message": self.message,
            "delivery_channel": self.delivery_channel,
            "priority": self.priority,
            "is_read": self.is_read,
            "is_sent": self.is_sent,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "read_at": self.read_at.isoformat() if self.read_at else None,
            "metadata": self.notification_metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CustomWorkflow(Base):
    """
    Custom Workflow Model - User-created multi-agent workflow definitions.

    File-based WORKFLOW.md definitions remain supported separately. This table
    holds workflows created/edited via the Workflow Builder UI.
    """

    __tablename__ = 'custom_workflows'

    workflow_id: Mapped[str] = mapped_column(String(100), primary_key=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    use_case: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    trigger_examples: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default='[]'
    )
    phases: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default='[]'
    )
    graph_layout: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default='{}'
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default='now()',
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default='now()',
    )

    __table_args__ = (
        Index('idx_custom_workflows_active', 'is_active'),
        Index('idx_custom_workflows_created_by', 'created_by'),
        Index('idx_custom_workflows_name', 'name'),
    )

    def to_dict(self) -> dict:
        """Convert custom workflow to dictionary."""
        return {
            'workflow_id': self.workflow_id,
            'name': self.name,
            'description': self.description,
            'use_case': self.use_case,
            'trigger_examples': self.trigger_examples or [],
            'phases': self.phases or [],
            'graph_layout': self.graph_layout or {},
            'is_active': self.is_active,
            'created_by': self.created_by,
            'version': self.version,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class Skill(Base):
    """Skill model - reusable, parameterized SOC capability (detection,
    enrichment, response, reporting) that agents and workflows can invoke."""

    __tablename__ = 'skills'

    skill_id: Mapped[str] = mapped_column(String(32), primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)

    # JSON Schema for skill parameters (the inputs the skill accepts).
    input_schema: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default='{}'
    )
    # JSON Schema for skill output.
    output_schema: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default='{}'
    )
    # MCP tool names required by this skill
    # (e.g. ["splunk.search", "virustotal.hash_lookup"]).
    required_tools: Mapped[List[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default='[]'
    )
    # LLM instructions; may contain {{param}} placeholders.
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    # Ordered execution steps (tool calls / prompts / transforms) — interpreted
    # by the future skill-execution worker.
    execution_steps: Mapped[List[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default='[]'
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default='true'
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default='1'
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        server_default='now()',
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default='now()',
    )

    __table_args__ = (
        Index('idx_skill_category', 'category'),
        Index('idx_skill_is_active', 'is_active'),
        Index(
            'idx_skill_name_trgm',
            'name',
            postgresql_ops={'name': 'gin_trgm_ops'},
            postgresql_using='gin',
        ),
    )

    @staticmethod
    def generate_skill_id() -> str:
        """Generate a new skill_id in the form s-YYYYMMDD-XXXXXXXX."""
        ts = datetime.utcnow().strftime('%Y%m%d')
        return f"s-{ts}-{uuid.uuid4().hex[:8].upper()}"

    def to_dict(self) -> dict:
        """Convert skill to dictionary."""
        return {
            'skill_id': self.skill_id,
            'name': self.name,
            'description': self.description,
            'category': self.category,
            'input_schema': self.input_schema or {},
            'output_schema': self.output_schema or {},
            'required_tools': self.required_tools or [],
            'prompt_template': self.prompt_template,
            'execution_steps': self.execution_steps or [],
            'is_active': self.is_active,
            'created_by': self.created_by,
            'version': self.version,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class CustomAgent(Base):
    """User-defined SOC agent created via the Agent Builder UI."""

    __tablename__ = 'custom_agents'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    icon: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    color: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    specialization: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    role: Mapped[str] = mapped_column(Text, nullable=False)
    extra_principles: Mapped[str] = mapped_column(Text, nullable=False, default='', server_default='')
    methodology: Mapped[str] = mapped_column(Text, nullable=False, default='', server_default='')
    system_prompt_override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    recommended_tools: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default='[]')
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096, server_default='4096')
    enable_thinking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, server_default='now()'
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow,
        onupdate=datetime.utcnow, server_default='now()'
    )

    __table_args__ = (
        Index('idx_custom_agents_updated_at', 'updated_at'),
    )

    def to_dict(self) -> dict:
        """Convert custom agent to dictionary."""
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'icon': self.icon,
            'color': self.color,
            'specialization': self.specialization,
            'role': self.role,
            'extra_principles': self.extra_principles,
            'methodology': self.methodology,
            'system_prompt_override': self.system_prompt_override,
            'recommended_tools': self.recommended_tools or [],
            'max_tokens': self.max_tokens,
            'enable_thinking': self.enable_thinking,
            'model': self.model,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
