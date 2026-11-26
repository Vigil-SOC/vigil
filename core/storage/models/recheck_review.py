"""Append-only blind reviews of a capped re-check (#5095).

The DDL is ``core/upgrade/versions/0124_recheck_review.sql``. A later call is
another insert. Nothing updates the first.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class RecheckReview(Base):
    """One person's call on one drawn ticket. The earliest row is the call."""

    __tablename__ = "recheck_reviews"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("capped_recheck_tickets.id", ondelete="CASCADE"),
        nullable=False,
    )
    call: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
