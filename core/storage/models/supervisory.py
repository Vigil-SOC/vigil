"""The supervisory model's suggestions and the append-only history of what people did.

The DDL is ``core/upgrade/versions/0125_supervisory_suggestions.sql`` and
``0126_supervisory_worker_state.sql``.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class SupervisorySuggestion(Base):
    """One suggested change to one setting, with the card it was made with."""

    __tablename__ = "supervisory_suggestions"

    suggestion_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fn: Mapped[str] = mapped_column(Text, nullable=False)
    setting_key: Mapped[str] = mapped_column(Text, nullable=False)
    verb: Mapped[str] = mapped_column(Text, nullable=False)
    from_value: Mapped[str] = mapped_column(Text, nullable=False)
    to_value: Mapped[str] = mapped_column(Text, nullable=False)
    widens_autonomy: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    card: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    held_incident: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SupervisoryHistory(Base):
    """One apply, undo or dismissal. Only the follow-up writes back: improved and note."""

    __tablename__ = "supervisory_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    suggestion_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("supervisory_suggestions.suggestion_id", ondelete="RESTRICT"),
        nullable=False,
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    setting_key: Mapped[str] = mapped_column(Text, nullable=False)
    from_value: Mapped[str] = mapped_column(Text, nullable=False)
    to_value: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    actor_principal_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # applied only: whether results improved after. Null until the follow-up reads it.
    improved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class SupervisoryWorkerState(Base):
    """When the scheduled check last ran and when the next is due. One row, id 1."""

    __tablename__ = "supervisory_worker_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    last_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_error: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
