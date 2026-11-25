"""History import ORM models (#4886, #5091, #5092): an import's record, its
tickets and its file alerts.

The DDL (core/upgrade/versions/0106_history_imports.sql,
0120_history_alerts.sql and 0121_history_ticket_alert_ids.sql) states the
domains.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class HistoryImport(Base):
    """One import of closed tickets and alerts, and how its last run went."""

    __tablename__ = "history_imports"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error: Mapped[Optional[str]] = mapped_column(Text)
    rows_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tickets_kept: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verdicts_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sightings_written: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    left_out: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    bulk_not_tested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    severity_unknown: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unmapped: Mapped[Dict[str, int]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    # {column name: how many alert rows carried it}, for names not stored.
    dropped_columns: Mapped[Dict[str, int]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    pull_days: Mapped[Optional[int]] = mapped_column(Integer)
    pulled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    concluded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    has_ticket_detail: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )


class HistoryTicket(Base):
    """One kept ticket of an import, bulk-closed included. Holds no closer."""

    __tablename__ = "history_tickets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_id: Mapped[str] = mapped_column(
        Text, ForeignKey("history_imports.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_id: Mapped[Optional[str]] = mapped_column(Text)
    source_system: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    disposition_raw: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    rule_id: Mapped[Optional[str]] = mapped_column(Text)
    entity_keys: Mapped[List[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    # Alert ids the ticket named. Empty for a connected pull and for an
    # import stored before the column existed.
    alert_ids: Mapped[List[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    notes_fingerprint: Mapped[Optional[str]] = mapped_column(Text)
    notes_empty: Mapped[bool] = mapped_column(Boolean, nullable=False)
    opened_missing: Mapped[bool] = mapped_column(Boolean, nullable=False)
    closed_approximate: Mapped[bool] = mapped_column(Boolean, nullable=False)
    bulk_closed: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "import_id", "position", name="history_tickets_import_position_key"
        ),
    )


class HistoryAlert(Base):
    """One file alert, keyed by the source's alert id. Holds no leftover columns."""

    __tablename__ = "history_alerts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    import_id: Mapped[str] = mapped_column(
        Text, ForeignKey("history_imports.id", ondelete="CASCADE"), nullable=False
    )
    alert_id: Mapped[str] = mapped_column(Text, nullable=False)
    alert_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rule_id: Mapped[Optional[str]] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    entity_keys: Mapped[List[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )

    __table_args__ = (
        UniqueConstraint(
            "import_id", "alert_id", name="history_alerts_import_alert_key"
        ),
    )
