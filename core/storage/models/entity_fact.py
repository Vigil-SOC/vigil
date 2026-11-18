"""What a source said about an entity, kept for its time to live (#4741)."""

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class EntityFact(Base):
    """One row per (Entity Key, source, Case or none); revision 0096_entity_facts.

    ``facts`` holds only the fields the source's projection in
    ``core/integrations/entity_facts.py`` allowlists. Naive UTC (core/time.py).
    """

    __tablename__ = "entity_facts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    # NULL for a triage read; a run on a Case writes its own row (#4742).
    case_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=True
    )
    facts: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    __table_args__ = (
        Index(
            "uq_entity_facts_key_source_case",
            "entity_key",
            "source",
            func.coalesce(case_id, ""),
            unique=True,
        ),
        Index("idx_entity_facts_expires", "expires_at"),
        Index("idx_entity_facts_fetched", "fetched_at"),
    )
