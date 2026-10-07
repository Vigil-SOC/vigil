"""How long since each federation source last pulled successfully."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from core.storage.connection import get_db_manager
from core.storage.models import FederationSource
from core.time import utcnow


def source_collection_lag(now: Optional[datetime] = None) -> list[dict]:
    """``now - last_success_at`` per ``federation_sources`` row.

    Quiet when ``last_success_at`` is null, or the age exceeds
    ``interval_seconds``. A quiet source is one the poller has not kept up.
    """
    if now is None:
        now = utcnow()
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    db = get_db_manager()
    with db.session_scope() as session:
        rows = (
            session.query(FederationSource).order_by(FederationSource.source_id).all()
        )
        result = []
        for row in rows:
            success = row.last_success_at
            if success is not None and success.tzinfo is not None:
                success = success.replace(tzinfo=None)
            interval = int(row.interval_seconds)
            if success is None:
                result.append(
                    {
                        "source_id": row.source_id,
                        "lag_seconds": None,
                        "quiet": True,
                        "interval_seconds": interval,
                    }
                )
                continue
            age = (now - success).total_seconds()
            result.append(
                {
                    "source_id": row.source_id,
                    "lag_seconds": age,
                    "quiet": age > interval,
                    "interval_seconds": interval,
                }
            )
    return result
