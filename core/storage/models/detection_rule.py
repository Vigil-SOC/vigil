"""Detection rule ORM models.

The schema is core/upgrade/versions/0026_detection_rules.sql,
0027_detection_rule_checks.sql, 0028_detection_rule_origin.sql,
0029_detection_rule_stages.sql, 0031_detection_rule_changes.sql and
0033_detection_rule_known_name.sql; these models
mirror it for core/detections/inventory.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base

# disabled: a SIEM rule that is listed but switched off.
DETECTION_RULE_STATUSES = ("active", "disabled", "removed")
DETECTION_RULE_STAGES = ("shadow", "live", "off")
# How a SIEM copy's origin was decided; 'same_id' and 'known_name' are the sync's own.
LINK_KINDS = ("same_id", "known_name", "agent", "declined")


class DetectionRule(Base):
    """One copy of a rule, keyed by its source and the source's own key for it."""

    __tablename__ = "detection_rules"
    __table_args__ = (
        UniqueConstraint(
            "source", "native_key", name="detection_rules_source_native_key"
        ),
        CheckConstraint(
            "status IN ('active', 'disabled', 'removed')",
            name="detection_rules_status_check",
        ),
        CheckConstraint(
            "latest_version >= 1", name="detection_rules_latest_version_check"
        ),
        CheckConstraint(
            "(link_kind IN ('same_id', 'known_name', 'agent', 'declined')"
            " AND origin_rule_id IS NOT NULL)"
            " OR (link_kind IS NULL AND origin_rule_id IS NULL)",
            name="detection_rules_link_kind_check",
        ),
        Index("idx_detection_rules_techniques", "techniques", postgresql_using="gin"),
        Index("idx_detection_rules_origin_rule_id", "origin_rule_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    native_key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    techniques: Mapped[List[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    tactics: Mapped[List[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A SIEM copy's repo rule. With link_kind 'declined' it is the pair refused.
    origin_rule_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("detection_rules.id"), nullable=True
    )
    link_kind: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    linked_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    link_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    linked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DetectionRuleVersion(Base):
    """One version of a rule's logic. Append-only but for ``checks``."""

    __tablename__ = "detection_rule_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="detection_rule_versions_version_check"),
    )

    rule_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("detection_rules.id"), primary_key=True
    )
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # {"runs": {result, reason, checked_at}}; empty until a check is recorded.
    checks: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DetectionRuleStage(Base):
    """One stage an overlay rule entered. Append-only; the latest row is current."""

    __tablename__ = "detection_rule_stages"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('shadow', 'live', 'off')",
            name="detection_rule_stages_stage_check",
        ),
        CheckConstraint("version >= 1", name="detection_rule_stages_version_check"),
        Index("idx_detection_rule_stages_rule", "rule_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("detection_rules.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    proof: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DetectionRuleCheckChange(Base):
    """A rule's check result as sync recorded it changing. Append-only.

    ``check_key`` is 'runs', 'sees' or 'status'; ``old_result`` is the previous row's
    ``new_result`` for that rule and check, any version, or None on first sight.
    """

    __tablename__ = "detection_rule_check_changes"
    __table_args__ = (
        Index(
            "idx_detection_rule_check_changes_latest",
            "rule_id",
            "check_key",
            text("changed_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("detection_rules.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    check_key: Mapped[str] = mapped_column(Text, nullable=False)
    old_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    new_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
