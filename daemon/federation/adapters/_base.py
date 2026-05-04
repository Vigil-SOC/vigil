"""Shared helpers for federation adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


def parse_cursor_since(cursor: Dict[str, Any]) -> Optional[datetime]:
    """Read the ``last_poll_at`` ISO timestamp from cursor, if present.

    Returns ``None`` for first-run (empty cursor) — adapters MUST treat that
    as "from now" rather than backfilling, per the federation MVP design.
    """
    raw = cursor.get("last_poll_at") if cursor else None
    if not raw:
        return None
    try:
        # Drop trailing Z if present (datetime.fromisoformat doesn't accept it
        # before 3.11 in all cases).
        if isinstance(raw, str) and raw.endswith("Z"):
            raw = raw[:-1]
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def fresh_cursor() -> Dict[str, Any]:
    """Cursor value to persist after a successful fetch."""
    return {"last_poll_at": datetime.utcnow().isoformat()}
