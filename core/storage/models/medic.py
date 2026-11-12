"""When the backend last reached Medic (C5 §5.3, D2-18)."""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class MedicLastSeen(Base):
    """One row, written only by the backend's poller (core/platform/medic_last_seen.py).

    Every time is the backend's clock. ``infra/database/init/44_medic_last_seen.sql``
    builds the same table; ``python -m core.upgrade`` adds it to an adopted database.
    """

    __tablename__ = "medic_last_seen"
    __table_args__ = (
        CheckConstraint("id = 1", name="medic_last_seen_one_row"),
        CheckConstraint(
            "failure_kind IN ('refused', 'timeout', '401', '5xx')",
            name="medic_last_seen_failure_kind",
        ),
        CheckConstraint(
            "jsonb_typeof(status_snapshot) = 'object' "
            "AND octet_length(status_snapshot::text) <= 4096",
            name="medic_last_seen_snapshot_typed",
        ),
    )

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, default=1, server_default=text("1")
    )
    # The last successful status poll, and the first failure since then.
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    first_failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # The latest failure: refused | timeout | 401 | 5xx. Null while Medic answers.
    failure_kind: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # Typed fields from the last good status answer, at most 4 KiB.
    status_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


_WATCH_WORDS = (
    "'down', 'blind', 'not_recording', 'crash_looping', 'running', 'degraded'"
)


class MedicWatchState(Base):
    """The status word told to medic.admin, and the polls confirming the next one.

    One row, written only by the watch in ``core/platform/medic_last_seen.py``.
    ``0065_medic_watch.sql`` builds the same table.
    """

    __tablename__ = "medic_watch_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="medic_watch_state_one_row"),
        CheckConstraint(
            f"announced_word IS NULL OR announced_word IN ({_WATCH_WORDS})",
            name="medic_watch_state_announced",
        ),
        CheckConstraint(
            f"confirming_word IS NULL OR confirming_word IN ({_WATCH_WORDS})",
            name="medic_watch_state_confirming",
        ),
        CheckConstraint("confirming_polls >= 0", name="medic_watch_state_polls"),
    )

    id: Mapped[int] = mapped_column(
        SmallInteger, primary_key=True, default=1, server_default=text("1")
    )
    # The word a notice already went out for. Null until the first one.
    announced_word: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The word the consecutive-poll count is for. Running and degraded share it.
    confirming_word: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confirming_polls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # medic_last_seen.updated_at of the poll that last moved the count.
    counted_updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    # When the current red word was first read. The bell's "since HH:MM".
    red_since: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
