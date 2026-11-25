"""Capped re-check of the Day 1 list (#5093).

The DDL is ``core/upgrade/versions/0123_capped_recheck.sql``. A batch is
inserted once. The driver then writes the run id and a reason.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class CappedRecheckBatch(Base):
    """One draw. ``status`` is running, interrupted, complete, or a ceiling stop."""

    __tablename__ = "capped_recheck_batches"

    batch_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[Optional[str]] = mapped_column(Text)
    record_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("report_records.record_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    post_install_closures: Mapped[int] = mapped_column(Integer, nullable=False)
    control_count: Mapped[int] = mapped_column(Integer, nullable=False)
    resumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    quota_hold: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class CappedRecheckTicket(Base):
    """One drawn ticket, in the shuffled order stored at ``position``."""

    __tablename__ = "capped_recheck_tickets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("capped_recheck_batches.batch_id", ondelete="CASCADE"),
        nullable=False,
    )
    import_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_id: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_group: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    random_key: Mapped[str] = mapped_column(Text, nullable=False)
    import_finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    run_id: Mapped[Optional[str]] = mapped_column(String(80))
    dispatch_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    dispatch_token: Mapped[Optional[str]] = mapped_column(Text)
    reason: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "batch_id", "position", name="capped_recheck_tickets_batch_position_key"
        ),
    )
