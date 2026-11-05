"""Day-one cold start: one attempt at computing a baseline from existing history.

A run's alerts and tickets live in their own tables. They never enter ``findings``
or ``case_closure_info``, so a dry run cannot trigger triage or count as a closure.
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Mapping, Optional

from sqlalchemy import Boolean, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class RunStatus(str, Enum):
    COLLECTING = "collecting"
    JUDGING = "judging"
    READY = "ready"
    LIVE = "live"
    FAILED = "failed"


TRANSITIONS: Mapping[RunStatus, FrozenSet[RunStatus]] = {
    RunStatus.COLLECTING: frozenset({RunStatus.JUDGING, RunStatus.FAILED}),
    RunStatus.JUDGING: frozenset({RunStatus.READY, RunStatus.FAILED}),
    RunStatus.READY: frozenset({RunStatus.JUDGING, RunStatus.LIVE, RunStatus.FAILED}),
    # Back to ready when another run goes live: one live run at most.
    RunStatus.LIVE: frozenset({RunStatus.READY}),
    RunStatus.FAILED: frozenset(),
}

ACTIVE_STATUSES: FrozenSet[RunStatus] = frozenset(
    {RunStatus.COLLECTING, RunStatus.JUDGING}
)


class InvalidTransition(Exception):
    def __init__(self, current: RunStatus, wanted: RunStatus):
        super().__init__(f"A {current.value} run cannot become {wanted.value}.")
        self.current = current
        self.wanted = wanted


class ColdStartRun(Base):
    __tablename__ = "cold_start_runs"
    __table_args__ = (
        # In the database, not in Python: two API workers can race either rule.
        Index(
            "uq_cold_start_runs_one_active",
            text("(1)"),
            unique=True,
            postgresql_where=text("status IN ('collecting', 'judging')"),
        ),
        Index(
            "uq_cold_start_runs_one_live",
            text("(1)"),
            unique=True,
            postgresql_where=text("status = 'live'"),
        ),
    )

    run_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        SQLEnum(
            RunStatus,
            native_enum=False,
            create_constraint=False,
            length=16,
            values_callable=lambda members: [m.value for m in members],
        ),
        nullable=False,
        default=RunStatus.COLLECTING,
    )
    history_days: Mapped[int] = mapped_column(Integer, nullable=False)
    review_days: Mapped[int] = mapped_column(Integer, nullable=False)
    judge_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    # Baseline.to_dict(). Null until the first build.
    baseline: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    progress: Mapped[Dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    # {"alerts": {key: {reason: n}}, "tickets": {key: {reason: n}}}. Keyed by file
    # hash so uploading the same file twice does not count its drops twice.
    drops: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    judging_skipped: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )
    live_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    def move_to(self, status: RunStatus) -> None:
        if status not in TRANSITIONS[self.status]:
            raise InvalidTransition(self.status, status)
        self.status = status


class ColdStartAlert(Base):
    """A snapshot of one alert in a run. ``llm`` is
    ``{verdict, confidence, reasoning, parsed, latency_ms, model}`` or null."""

    __tablename__ = "cold_start_alerts"
    __table_args__ = (
        Index("idx_cold_start_alerts_review", "run_id", "in_review_window", "stage1"),
    )

    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cold_start_runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    alert_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    entities: Mapped[List[str]] = mapped_column(JSONB, nullable=False, default=list)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    in_review_window: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    stage1: Mapped[Optional[str]] = mapped_column(Text)
    rule: Mapped[Optional[str]] = mapped_column(Text)
    reasons: Mapped[Optional[List[str]]] = mapped_column(JSONB)
    llm: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB)
    review: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_by: Mapped[Optional[str]] = mapped_column(Text)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ColdStartOutcome(Base):
    """A closed ticket that passed label filtering.

    ``alert_ids`` link it to alerts in the run; tickets can arrive before the alerts
    do, so that link resolves at each build. ``signature`` is the source-and-title
    fallback for a ticket naming no alert the run has."""

    __tablename__ = "cold_start_outcomes"

    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("cold_start_runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    external_id: Mapped[str] = mapped_column(Text, primary_key=True)
    alert_ids: Mapped[List[str]] = mapped_column(JSONB, nullable=False, default=list)
    signature: Mapped[Optional[str]] = mapped_column(Text)
    disposition: Mapped[str] = mapped_column(Text, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entities: Mapped[List[str]] = mapped_column(JSONB, nullable=False, default=list)
    source_system: Mapped[Optional[str]] = mapped_column(Text)
