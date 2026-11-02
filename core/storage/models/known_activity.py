"""Known activity ORM model. Schema and rationale:
``core/upgrade/versions/0020_known_activity.sql``."""

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base
from core.time import utcnow

KNOWN_ACTIVITY_ORIGINS = ("ad_hoc", "finding", "case", "run")


class KnownActivity(Base):
    """One analyst's statement that an entity's activity is expected, until a date."""

    __tablename__ = "known_activity"

    known_activity_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    entity_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_value: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    removed_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    removal_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ad_hoc", server_default="ad_hoc"
    )
    origin_ref: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "entity_kind IN ('ip', 'domain', 'host', 'url', 'email', 'hash', 'arn', "
            "'aws_key', 'user', 'process')",
            name="ck_known_activity_kind",
        ),
        CheckConstraint(
            "origin IN ('ad_hoc', 'finding', 'case', 'run')",
            name="ck_known_activity_origin",
        ),
        CheckConstraint(
            "(removed_at IS NULL) = (removed_by IS NULL)",
            name="ck_known_activity_removal",
        ),
        CheckConstraint("expires_at > created_at", name="ck_known_activity_expiry"),
        Index(
            "idx_known_activity_entity",
            "entity_kind",
            "entity_value",
            postgresql_where=text("removed_at IS NULL"),
        ),
        Index("idx_known_activity_created_at", text("created_at DESC")),
    )
