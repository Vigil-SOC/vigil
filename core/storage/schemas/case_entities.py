"""Serialization schemas for the case sub-entity models."""

from typing import Any, Optional

from pydantic import Field

from core.storage.schemas.base import JsonList, OptDateTime, ORMSchema, StrList


class SLAPolicySchema(ORMSchema):
    """SLAPolicy."""

    policy_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    priority_level: Optional[str] = None
    response_time_hours: Optional[float] = None
    resolution_time_hours: Optional[float] = None
    business_hours_only: Optional[bool] = None
    escalation_rules: Optional[Any] = None
    notification_thresholds: Optional[list[str]] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class CaseSLASchema(ORMSchema):
    """CaseSLA."""

    sla_id: Optional[int] = None
    case_id: Optional[str] = None
    sla_policy_id: Optional[str] = None
    response_due: OptDateTime = None
    resolution_due: OptDateTime = None
    response_completed_at: OptDateTime = None
    response_sla_met: Optional[bool] = None
    resolution_completed_at: OptDateTime = None
    resolution_sla_met: Optional[bool] = None
    breached: Optional[bool] = None
    breach_time: OptDateTime = None
    breach_reason: Optional[str] = None
    is_paused: Optional[bool] = None
    paused_at: OptDateTime = None
    resumed_at: OptDateTime = None
    total_pause_duration: Optional[int] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class CaseCommentSchema(ORMSchema):
    """CaseComment."""

    comment_id: Optional[int] = None
    case_id: Optional[str] = None
    parent_comment_id: Optional[int] = None
    author: Optional[str] = None
    content: Optional[str] = None
    mentions: StrList = Field(default_factory=list)
    attachment_ids: StrList = Field(default_factory=list)
    is_edited: Optional[bool] = None
    is_deleted: Optional[bool] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class CaseWatcherSchema(ORMSchema):
    """CaseWatcher."""

    case_id: Optional[str] = None
    user_id: Optional[str] = None
    notification_preferences: Optional[Any] = None
    created_at: OptDateTime = None


class CaseEvidenceSchema(ORMSchema):
    """CaseEvidence."""

    evidence_id: Optional[int] = None
    case_id: Optional[str] = None
    evidence_type: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    file_hash_md5: Optional[str] = None
    file_hash_sha256: Optional[str] = None
    source: Optional[str] = None
    collected_by: Optional[str] = None
    collected_at: OptDateTime = None
    chain_of_custody: JsonList = Field(default_factory=list)
    analysis_results: Optional[Any] = None
    tags: StrList = Field(default_factory=list)
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class CaseIOCSchema(ORMSchema):
    """CaseIOC."""

    ioc_id: Optional[int] = None
    case_id: Optional[str] = None
    ioc_type: Optional[str] = None
    value: Optional[str] = None
    threat_level: Optional[str] = None
    confidence: Optional[float] = None
    source: Optional[str] = None
    first_seen: OptDateTime = None
    last_seen: OptDateTime = None
    enrichment_data: Optional[Any] = None
    reputation_score: Optional[float] = None
    tags: StrList = Field(default_factory=list)
    context: Optional[str] = None
    is_active: Optional[bool] = None
    is_false_positive: Optional[bool] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class CaseTaskSchema(ORMSchema):
    """CaseTask."""

    task_id: Optional[int] = None
    case_id: Optional[str] = None
    parent_task_id: Optional[int] = None
    title: Optional[str] = None
    description: Optional[str] = None
    assignee: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    due_date: OptDateTime = None
    completed_at: OptDateTime = None
    estimated_hours: Optional[float] = None
    actual_hours: Optional[float] = None
    checklist_items: JsonList = Field(default_factory=list)
    task_order: Optional[int] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class CaseTemplateSchema(ORMSchema):
    """CaseTemplate."""

    template_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    template_type: Optional[str] = None
    default_priority: Optional[str] = None
    default_status: Optional[str] = None
    default_sla_policy_id: Optional[str] = None
    task_templates: JsonList = Field(default_factory=list)
    playbook_steps: JsonList = Field(default_factory=list)
    applicable_mitre_techniques: StrList = Field(default_factory=list)
    tags: StrList = Field(default_factory=list)
    is_active: Optional[bool] = None
    usage_count: Optional[int] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class CaseRelationshipSchema(ORMSchema):
    """CaseRelationship."""

    relationship_id: Optional[int] = None
    case_id: Optional[str] = None
    related_case_id: Optional[str] = None
    relationship_type: Optional[str] = None
    created_by: Optional[str] = None
    notes: Optional[str] = None
    created_at: OptDateTime = None


class CaseMetricsSchema(ORMSchema):
    """CaseMetrics."""

    case_id: Optional[str] = None
    time_to_detect: Optional[int] = None
    time_to_respond: Optional[int] = None
    time_to_contain: Optional[int] = None
    time_to_resolve: Optional[int] = None
    total_work_hours: Optional[float] = None
    analyst_handoffs_count: Optional[int] = None
    sla_met: Optional[bool] = None
    response_sla_met: Optional[bool] = None
    resolution_sla_met: Optional[bool] = None
    comment_count: Optional[int] = None
    evidence_count: Optional[int] = None
    ioc_count: Optional[int] = None
    task_count: Optional[int] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class CaseAttachmentSchema(ORMSchema):
    """CaseAttachment."""

    attachment_id: Optional[int] = None
    case_id: Optional[str] = None
    filename: Optional[str] = None
    file_path: Optional[str] = None
    file_type: Optional[str] = None
    file_size: Optional[int] = None
    mime_type: Optional[str] = None
    uploaded_by: Optional[str] = None
    description: Optional[str] = None
    tags: StrList = Field(default_factory=list)
    virus_scan_result: Optional[str] = None
    scan_details: Optional[Any] = None
    created_at: OptDateTime = None


class CaseClosureInfoSchema(ORMSchema):
    """CaseClosureInfo."""

    case_id: Optional[str] = None
    closure_category: Optional[str] = None
    root_cause: Optional[str] = None
    contributing_factors: StrList = Field(default_factory=list)
    lessons_learned: Optional[str] = None
    recommendations: Optional[str] = None
    recurrence_prevention: Optional[str] = None
    false_positive_reason: Optional[str] = None
    executive_summary: Optional[str] = None
    closed_by: Optional[str] = None
    closure_notes: Optional[str] = None
    closed_at: OptDateTime = None


class CaseEscalationSchema(ORMSchema):
    """CaseEscalation."""

    escalation_id: Optional[int] = None
    case_id: Optional[str] = None
    escalated_from: Optional[str] = None
    escalated_to: Optional[str] = None
    reason: Optional[str] = None
    urgency_level: Optional[str] = None
    status: Optional[str] = None
    escalated_at: OptDateTime = None
    acknowledged_at: OptDateTime = None
    resolved_at: OptDateTime = None
    resolution_notes: Optional[str] = None


class CaseAuditLogSchema(ORMSchema):
    """CaseAuditLog."""

    audit_id: Optional[int] = None
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    action: Optional[str] = None
    field_name: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    change_summary: Optional[str] = None
    changed_by: Optional[str] = None
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    timestamp: OptDateTime = None


class CaseNotificationSchema(ORMSchema):
    """CaseNotification."""

    notification_id: Optional[int] = None
    case_id: Optional[str] = None
    user_id: Optional[str] = None
    notification_type: Optional[str] = None
    title: Optional[str] = None
    message: Optional[str] = None
    delivery_channel: Optional[str] = None
    priority: Optional[str] = None
    is_read: Optional[bool] = None
    is_sent: Optional[bool] = None
    sent_at: OptDateTime = None
    read_at: OptDateTime = None
    metadata: Optional[Any] = Field(
        default=None, validation_alias="notification_metadata"
    )
    created_at: OptDateTime = None
