"""A team owns cases. Membership says who is on it and grants nothing."""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base
from core.time import utcnow


class Team(Base):
    """One named set of people. Names are unique ignoring case, archived included.

    Exactly one row has ``is_default``. That row is created by the upgrade, and
    it can be renamed, never archived. ``archived_at`` set means the team
    accepts neither cases nor members.
    """

    __tablename__ = "teams"

    team_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )

    __table_args__ = (
        Index("uq_teams_name", text("lower(name)"), unique=True),
        Index(
            "uq_teams_is_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
        ),
    )

    @property
    def id(self) -> str:
        """The id case reads use. The column stays ``team_id``."""
        return self.team_id


class TeamMembership(Base):
    """A person principal on a team. Service accounts and agents never join."""

    __tablename__ = "team_memberships"

    team_id: Mapped[str] = mapped_column(
        String(50),
        ForeignKey("teams.team_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    principal_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("principals.principal_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )

    __table_args__ = (Index("idx_team_memberships_principal", "principal_id"),)
