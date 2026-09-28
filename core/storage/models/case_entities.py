"""Case-adjacent ORM models (SLA, comments, evidence, tasks, ...)."""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base, JSONBList
from core.time import utcnow


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )

    # Indexes
    __table_args__ = (
        Index("idx_sla_policy_priority", "priority_level"),
        Index("idx_sla_policy_active", "is_active"),
        Index("idx_sla_policy_default", "is_default"),
    )


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_sla_case_id", "case_id"),
        Index("idx_case_sla_policy_id", "sla_policy_id"),
        Index("idx_case_sla_response_due", "response_due"),
        Index("idx_case_sla_resolution_due", "resolution_due"),
        Index("idx_case_sla_breached", "breached"),
    )


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_comment_case_id", "case_id"),
        Index("idx_case_comment_author", "author"),
        Index("idx_case_comment_parent_id", "parent_comment_id"),
        Index("idx_case_comment_created_at", "created_at"),
    )


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_watcher_case_id", "case_id"),
        Index("idx_case_watcher_user_id", "user_id"),
    )


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
        JSONBList, nullable=False, default=list
    )

    # Analysis results
    analysis_results: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Tags
    tags: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String), nullable=True)

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

    # Indexes
    __table_args__ = (
        Index("idx_case_evidence_case_id", "case_id"),
        Index("idx_case_evidence_type", "evidence_type"),
        Index("idx_case_evidence_collected_by", "collected_by"),
        Index("idx_case_evidence_collected_at", "collected_at"),
    )


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_ioc_case_id", "case_id"),
        Index("idx_case_ioc_type", "ioc_type"),
        Index("idx_case_ioc_value", "value"),
        Index("idx_case_ioc_threat_level", "threat_level"),
        Index("idx_case_ioc_is_active", "is_active"),
    )


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_task_case_id", "case_id"),
        Index("idx_case_task_parent_id", "parent_task_id"),
        Index("idx_case_task_assignee", "assignee"),
        Index("idx_case_task_status", "status"),
        Index("idx_case_task_due_date", "due_date"),
    )


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
        JSONB, nullable=False, default=list
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
    # server_default too: this table exists only through create_all, and the
    # 05_case_management_extended.sql seed writes it with raw SQL.
    usage_count: Mapped[int] = mapped_column(
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

    # Indexes
    __table_args__ = (
        Index("idx_case_template_type", "template_type"),
        Index("idx_case_template_active", "is_active"),
        Index("idx_case_template_usage_count", "usage_count"),
    )


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_relationship_case_id", "case_id"),
        Index("idx_case_relationship_related_case_id", "related_case_id"),
        Index("idx_case_relationship_type", "relationship_type"),
    )


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_attachment_case_id", "case_id"),
        Index("idx_case_attachment_uploaded_by", "uploaded_by"),
        Index("idx_case_attachment_created_at", "created_at"),
    )


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
    # Which kind of actor concluded, recorded at the close rather than inferred
    # from the name afterwards. This is what episodic memory reads as Trust, and
    # a name cannot answer it: an agent closing as "soc-automation" and a person
    # closing as "nestor" are indistinguishable to a lookup.
    closed_by_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="agent", server_default="agent"
    )
    closure_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Timestamps
    closed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_audit_entity_type", "entity_type"),
        Index("idx_case_audit_entity_id", "entity_id"),
        Index("idx_case_audit_changed_by", "changed_by"),
        Index("idx_case_audit_timestamp", "timestamp"),
        Index("idx_case_audit_action", "action"),
    )


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
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
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
