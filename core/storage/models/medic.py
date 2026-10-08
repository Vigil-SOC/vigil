"""When the backend last reached Medic (C5 §5.3, D2-18)."""

from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base


class MedicLastSeen(Base):
    """One row, written only by the backend's poller (core/platform/medic_last_seen.py).

    Every time is the backend's clock. ``infra/database/init/41_medic_last_seen.sql``
    and ``scripts/migrate_schema.py`` build the same table for existing databases.
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
