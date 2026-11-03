"""Rule inventory ORM models.

The schema is core/upgrade/versions/0024_rule_inventory.sql; these models
mirror it for the inventory sync in core/detections/inventory.
"""

from datetime import datetime
from typing import Any, Dict, List

from sqlalchemy import (
    ARRAY,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base

RULE_STATUSES = ("active", "disabled", "removed")


class Rule(Base):
    """One rule from one source, under a stable id like ``R-42``."""

    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        server_default=text("('R-' || nextval('public.rules_number_seq')::text)"),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    native_key: Mapped[str] = mapped_column(Text, nullable=False)
    techniques: Mapped[List[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_changed: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("source", "native_key", name="rules_source_native_key"),
    )


class RuleVersion(Base):
    """One version of a rule's logic. Append-only."""

    __tablename__ = "rule_versions"

    rule_id: Mapped[str] = mapped_column(Text, ForeignKey("rules.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seen_in: Mapped[str] = mapped_column(Text, nullable=False)
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
