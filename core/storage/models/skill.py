"""Skill usage: one row per successful read of a skill body (#1560)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class SkillRead(Base):
    """A read of a skill's body through the ``read_skill`` tool.

    Why the table exists, why ``agent_id`` is nullable and why rows live
    8 days is stated once, in ``infra/database/init/40_skill_reads.sql``.
    Written and read by ``core/skills/skill_usage.py``.
    """

    __tablename__ = "skill_reads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("idx_skill_reads_skill_time", "skill_name", "read_at"),)
