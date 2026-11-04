"""Detection rule ORM models.

The schema is core/upgrade/versions/0026_detection_rules.sql; these models
mirror it for core/detections/inventory.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
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

DETECTION_RULE_STATUSES = ("active", "removed")


class DetectionRule(Base):
    """One copy of a rule, keyed by its source and the source's own key for it."""

    __tablename__ = "detection_rules"
    __table_args__ = (
        UniqueConstraint(
            "source", "native_key", name="detection_rules_source_native_key"
        ),
        CheckConstraint(
            "status IN ('active', 'removed')", name="detection_rules_status_check"
        ),
        CheckConstraint(
            "latest_version >= 1", name="detection_rules_latest_version_check"
        ),
        Index("idx_detection_rules_techniques", "techniques", postgresql_using="gin"),
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


class DetectionRuleVersion(Base):
    """One version of a rule's logic. Append-only."""

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
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
