"""Telemetry events kept by Vigil for the Logs page: one row per event, every
source in one schema, with the source's own record kept beside it.

Built by ``0017_log_events`` on an upgraded install and by create_all on a
fresh one; the two must agree."""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class LogEvent(Base):
    __tablename__ = "log_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The sender's key, e.g. sysmon, zeek, paloalto, dns, winsec, crowdstrike
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    event_class: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    src_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    src_host: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    dst_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    dst_host: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    dst_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    user_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    action: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mitre: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # LogLM anomaly score, 0..1, for the events it scored
    loglm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # A case or incident id the sender tied the event to
    incident: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    tags: Mapped[List[str]] = mapped_column(JSONB, nullable=False, default=list)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Lower-cased summary, hosts, user and raw record, for free-text search
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        Index("ix_log_events_time", "time"),
        Index("ix_log_events_source_time", "source", "time"),
        Index("ix_log_events_incident", "incident"),
    )
