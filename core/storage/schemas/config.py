"""Serialization schemas for the configuration and threat-intel models."""

from typing import Any, Optional

from pydantic import Field

from core.storage.schemas.base import OptDateTime, ORMSchema, StrList


class SystemConfigSchema(ORMSchema):
    """SystemConfig."""

    key: Optional[str] = None
    value: Optional[Any] = None
    description: Optional[str] = None
    config_type: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class UserPreferenceSchema(ORMSchema):
    """UserPreference."""

    user_id: Optional[str] = None
    preferences: Optional[Any] = None
    display_name: Optional[str] = None
    email: Optional[str] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None
    last_login: OptDateTime = None


class IntegrationConfigSchema(ORMSchema):
    """IntegrationConfig."""

    integration_id: Optional[str] = None
    enabled: Optional[bool] = None
    config: Optional[Any] = None
    integration_name: Optional[str] = None
    integration_type: Optional[str] = None
    description: Optional[str] = None
    last_test_at: OptDateTime = None
    last_test_success: Optional[bool] = None
    last_error: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class ConfigAuditLogSchema(ORMSchema):
    """ConfigAuditLog."""

    id: Optional[int] = None
    config_type: Optional[str] = None
    config_key: Optional[str] = None
    action: Optional[str] = None
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None
    changed_by: Optional[str] = None
    change_reason: Optional[str] = None
    timestamp: OptDateTime = None


class FederationSourceSchema(ORMSchema):
    """FederationSource."""

    source_id: Optional[str] = None
    enabled: Optional[bool] = None
    interval_seconds: Optional[int] = None
    max_items: Optional[int] = None
    min_severity: Optional[str] = None
    cursor: Optional[Any] = None
    last_poll_at: OptDateTime = None
    last_success_at: OptDateTime = None
    last_error: Optional[str] = None
    consecutive_errors: Optional[int] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class SharedIOCSchema(ORMSchema):
    """SharedIOC."""

    id: Optional[int] = None
    investigation_id: Optional[str] = None
    ioc_type: Optional[str] = None
    value: Optional[str] = None
    created_at: OptDateTime = None


class ThreatIndicatorSchema(ORMSchema):
    """ThreatIndicator."""

    id: Optional[int] = None
    indicator_type: Optional[str] = None
    indicator_value: Optional[str] = None
    source: Optional[str] = None
    collection_id: Optional[str] = None
    confidence: Optional[float] = None
    threat_level: Optional[str] = None
    labels: StrList = Field(default_factory=list)
    valid_from: OptDateTime = None
    valid_until: OptDateTime = None
    first_seen: OptDateTime = None
    last_seen: OptDateTime = None


class SketchMappingSchema(ORMSchema):
    """SketchMapping."""

    id: Optional[int] = None
    case_id: Optional[str] = None
    finding_id: Optional[str] = None
    sketch_id: Optional[int] = None
    sketch_name: Optional[str] = None
    sketch_url: Optional[str] = None
    created_at: OptDateTime = None


class AttackLayerSchema(ORMSchema):
    """AttackLayer."""

    id: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    layer_data: Optional[Any] = None
    case_id: Optional[str] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None
