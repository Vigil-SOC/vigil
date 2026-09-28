"""Case ORM model."""

from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import (
    ARRAY,
    DateTime,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)

from core.storage.models.base import Base, JSONBList, case_findings
from core.time import utcnow

if TYPE_CHECKING:
    from core.storage.models.finding import Finding


class Case(Base):
    """Case model - represents an investigation case grouping related findings."""

    __tablename__ = "cases"

    # Primary key
    case_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Basic case information
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, default="")

    # Status and priority
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="new", server_default="new"
    )
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium", server_default="medium"
    )

    # Assignment
    assignee: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Tags (array of strings)
    tags: Mapped[List[str]] = mapped_column(ARRAY(String), nullable=True, default=list)

    # Notes (JSONB array)
    notes: Mapped[List[dict]] = mapped_column(JSONBList, nullable=True, default=list)

    # Timeline events (JSONB array)
    timeline: Mapped[List[dict]] = mapped_column(
        JSONBList, nullable=False, default=list
    )

    # Activities (JSONB array)
    activities: Mapped[Optional[List[dict]]] = mapped_column(
        JSONBList, nullable=True, default=list
    )

    # Resolution steps (JSONB array)
    resolution_steps: Mapped[Optional[List[dict]]] = mapped_column(
        JSONBList, nullable=True, default=list
    )

    # MITRE ATT&CK techniques
    mitre_techniques: Mapped[Optional[List[str]]] = mapped_column(
        ARRAY(String), nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=text("now()"),
    )

    # Relationships
    findings: Mapped[List["Finding"]] = relationship(
        "Finding", secondary=case_findings, back_populates="cases", lazy="selectin"
    )

    # Indexes
    __table_args__ = (
        Index("idx_case_status", "status"),
        Index("idx_case_priority", "priority"),
        Index("idx_case_assignee", "assignee"),
        Index("idx_case_created_at", "created_at"),
        Index("idx_case_updated_at", "updated_at"),
    )
