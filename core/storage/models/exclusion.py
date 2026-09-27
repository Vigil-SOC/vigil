"""Analyst IP exclusion ORM model. Schema and rationale:
``infra/database/init/35_ip_exclusions.sql``."""

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base
from core.time import utcnow

EXCLUSION_ORIGINS = ("ad_hoc", "finding", "case", "run")


class IpExclusion(Base):
    """One address an analyst hid from the findings queue and run seeds."""

    __tablename__ = "ip_exclusions"

    exclusion_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ad_hoc", server_default="ad_hoc"
    )
    origin_ref: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    removed_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    removal_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "origin IN ('ad_hoc', 'finding', 'case', 'run')",
            name="ck_ip_exclusions_origin",
        ),
        CheckConstraint(
            "(removed_at IS NULL) = (removed_by IS NULL)",
            name="ck_ip_exclusions_removal",
        ),
        Index(
            "uniq_ip_exclusions_active_ip",
            "ip",
            unique=True,
            postgresql_where=text("removed_at IS NULL"),
        ),
        Index("idx_ip_exclusions_created_at", text("created_at DESC")),
    )
