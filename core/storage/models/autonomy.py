"""The stored effect class of each outside tool (#3304)."""

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
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
    # A remote server's tool: the name, description and input schema it was
    # classified against, and that definition's hash (#5300, revision 0133).
    # none_as_null: clearing writes SQL NULL, which the gates' IS NULL reads, not JSON null.
    pinned_definition: Mapped[Optional[dict]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    pinned_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A later definition that differs from the pin. Set means withdrawn: the
    # gates read the tool as unclassified until a person classifies it again.
    changed_definition: Mapped[Optional[dict]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # What capability it answers (#5312, revision 0135). NULL: the shipped
    # binding applies; {}: a person bound nothing; else {capability, server}.
    binding: Mapped[Optional[dict]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "effect_class IN ('read', 'read_unverified', 'reversible', 'irreversible')",
            name="tool_effect_classes_effect_class_check",
        ),
    )
