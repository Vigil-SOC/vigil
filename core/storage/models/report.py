"""Day 1 Report rows. Insert-only for vigil_app (0108_report_records)."""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base
from core.time import utcnow


class ReportRun(Base):
    """A Day 1 Report run's start. A start with no record did not complete."""

    __tablename__ = "report_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("report_runs_started_at_idx", "started_at"),)


class ReportRecord(Base):
    """One completed Day 1 Report. Never rewritten."""

    __tablename__ = "report_records"

    record_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("report_runs.run_id"), unique=True, nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    vigil_version: Mapped[str] = mapped_column(Text, nullable=False)
    modules: Mapped[list] = mapped_column(JSONB, nullable=False)
    checks: Mapped[list] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    preface: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("report_records_recorded_at_idx", "recorded_at"),)
