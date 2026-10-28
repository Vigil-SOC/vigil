"""The stored effect class of each outside tool (#3304)."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base
from core.time import utcnow


class ToolEffectClass(Base):
    """One outside tool name, as the registry flattens it, and what calling it does.

    A name with no row is unclassified. Vigil's own tools have no row. Built by
    ``core/upgrade/versions/0017_tool_effect_classes.sql``.
    """

    __tablename__ = "tool_effect_classes"

    tool_name: Mapped[str] = mapped_column(Text, primary_key=True)
    effect_class: Mapped[str] = mapped_column(String(20), nullable=False)
    # A principal id, or "system" for the seed and a server's destructiveHint.
    classified_by: Mapped[str] = mapped_column(Text, nullable=False)
    classified_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )

    __table_args__ = (
        CheckConstraint(
            "effect_class IN ('read', 'read_unverified', 'reversible', 'irreversible')",
            name="tool_effect_classes_effect_class_check",
        ),
    )
