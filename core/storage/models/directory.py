"""Groups pushed by the identity provider (#4424).

Membership is a principal id of an account that already exists. The rows
grant nothing: roles and teams are applied from the mapping, not from here.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base
from core.time import utcnow


class DirectoryGroup(Base):
    """One pushed group. ``external_id`` does not change; the label does."""

    __tablename__ = "directory_groups"

    group_id: Mapped[str] = mapped_column(Text, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )

    __table_args__ = (
        Index("uq_directory_groups_external", "external_id", unique=True),
    )


class DirectoryGroupMember(Base):
    """A principal in a pushed group."""

    __tablename__ = "directory_group_members"

    group_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("directory_groups.group_id", ondelete="CASCADE"),
        primary_key=True,
    )
    principal_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("principals.principal_id", ondelete="RESTRICT"),
        primary_key=True,
    )

    __table_args__ = (Index("idx_directory_group_members_principal", "principal_id"),)


class DirectoryPending(Base):
    """A pushed person whose username or email an existing account holds (#4507).

    Kept until an admin links it to that account, or a later create succeeds.
    """

    __tablename__ = "directory_pending"

    pending_id: Mapped[str] = mapped_column(Text, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )

    __table_args__ = (
        Index("uq_directory_pending_external", "external_id", unique=True),
    )
