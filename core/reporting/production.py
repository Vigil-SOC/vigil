"""Production Start: one system setting a person declares, never inferred.

``production.start`` holds ``started_at`` (the moment they name) and
``declared_at`` (when this server wrote it). Clearing stores both as null.
Who wrote it is only the ``config.change`` audit's ``changed_by``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from core.reporting.day1 import _clock_start
from core.storage.config_service import get_config_service, get_session
from core.time import utcnow

KEY = "production.start"
# A browser's "now" can sit a few seconds ahead of this server.
SKEW = timedelta(seconds=5)
_CONFIG_TYPE = "report"


class Rejected(Exception):
    """A time this server will not store. ``detail`` names the rule."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def read() -> dict[str, Optional[str]]:
    """The stored row, or both times null when it has not been written."""
    value = get_config_service().read_system_config(KEY)
    return _row(value if isinstance(value, dict) else None)


def declare(started_at: Optional[datetime], user_id: str) -> Optional[dict]:
    """Write the row as ``user_id``. None when the write failed.

    ``started_at`` of None clears both times. A time is accepted at or before
    now (plus a few seconds of skew) and not earlier than Day 1's clock start.
    ``declared_at`` is this server's now, except on a clear, when it is null.
    """
    if started_at is None:
        value = {"started_at": None, "declared_at": None}
        reason = "Production start cleared"
    else:
        started = _naive(started_at)
        now = utcnow()
        if started > now + SKEW:
            raise Rejected("started_at is in the future")
        with get_session() as session:
            earliest = _clock_start(session)
        if earliest is not None and started < _naive(earliest):
            raise Rejected(
                "started_at is earlier than when setup finished"
                " or the earliest person account was created"
            )
        value = {"started_at": _stamp(started), "declared_at": _stamp(now)}
        reason = "Production start declared"
    saved = get_config_service(user_id=user_id).set_system_config(
        key=KEY,
        value=value,
        description="When a person declared this install in production",
        config_type=_CONFIG_TYPE,
        change_reason=reason,
    )
    return value if saved else None


def _row(value: Optional[dict]) -> dict[str, Optional[str]]:
    if not value:
        return {"started_at": None, "declared_at": None}
    started = value.get("started_at")
    declared = value.get("declared_at")
    return {
        "started_at": started if isinstance(started, str) else None,
        "declared_at": declared if isinstance(declared, str) else None,
    }


def _naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds") + "Z"
