"""Rule link ORM model.

The schema is core/upgrade/versions/0025_rule_links.sql; core/detections/
inventory/links.py writes it.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import REAL, DateTime, ForeignKey, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base

# Most certain first.
LINK_HOWS = ("same_id", "vigil_tag", "known_name", "looks_same")
LINK_CONFIRMED = ("auto", "pending", "confirmed", "rejected")


class RuleLink(Base):
    """Two inventory rules that are copies of one rule. Stored once, smaller id first."""

    __tablename__ = "rule_links"

    rule_a: Mapped[str] = mapped_column(Text, ForeignKey("rules.id"), primary_key=True)
    rule_b: Mapped[str] = mapped_column(Text, ForeignKey("rules.id"), primary_key=True)
    how: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[Optional[float]] = mapped_column(REAL)
    evidence: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    found_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    confirmed_by: Mapped[Optional[str]] = mapped_column(Text)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
