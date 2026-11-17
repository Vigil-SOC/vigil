"""Skill usage: one row per successful read of a skill body (#1560)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
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


class SkillScan(Base):
    """The scan verdict for one skill folder's content, keyed by its digest.

    A change to any file is a different digest, so a verdict never covers other
    content. ``approved_by`` and ``approved_at`` mark a skill released by a
    person though the scan did not call it safe. The rule that reads this table
    is in ``core/skills/skill_hold.py``; the columns are explained in the
    migration that creates it.
    """

    __tablename__ = "skill_scans"

    digest: Mapped[str] = mapped_column(Text, primary_key=True)
    skill_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    recommendation: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_severity: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    findings: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    scanner_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Naive UTC, as everywhere else (core/time.py).
    scanned_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        server_default=text("(now() AT TIME ZONE 'utc')"),
    )
    # ``static`` or ``llm:<model>``; the hold's rule differs by mode. Null on
    # rows from before the column, which were all static.
    scan_mode: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(), nullable=True)

    __table_args__ = (
        CheckConstraint("status IN ('scanned', 'failed')", name="skill_scans_status"),
        CheckConstraint(
            "recommendation IS NULL OR recommendation IN ('SAFE', 'CAUTION', 'DO_NOT_INSTALL')",
            name="skill_scans_recommendation",
        ),
        Index("idx_skill_scans_name", "skill_name"),
    )


class SkillInstall(Base):
    """Where an operator skill installed from a git repo came from.

    One row per skill, keyed by its name: the repo and commit it was installed
    from and the digest of its content as installed, so a later edit in Vigil
    and a newer commit upstream can both be told. Written and read by
    ``core/skills/skill_sources.py``; the columns are explained in the
    migration that creates it.
    """

    __tablename__ = "skill_installs"

    skill_name: Mapped[str] = mapped_column(Text, primary_key=True)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    path: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    folder: Mapped[str] = mapped_column(Text, nullable=False)
    commit_sha: Mapped[str] = mapped_column(Text, nullable=False)
    digest: Mapped[str] = mapped_column(Text, nullable=False)
    installed_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Naive UTC, as everywhere else (core/time.py).
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(),
        nullable=False,
        server_default=text("(now() AT TIME ZONE 'utc')"),
    )

    __table_args__ = (Index("idx_skill_installs_repo", "repo_url", "ref", "path"),)
