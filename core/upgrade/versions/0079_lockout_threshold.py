"""Move a saved lockout threshold into 3–10 (#4450).

A saved 1 or 2 becomes 3, and 11–20 becomes 10. The document's version
increments and ``updated_by`` stays. One trail entry records the old and new
documents as the upgrade's act. A value already in 3–10, a missing policy, and
anything outside 1–20 stay as stored, so a second run changes nothing.
"""

from __future__ import annotations

import json
from decimal import Decimal

from alembic import op
from sqlalchemy import text

from core.audit import trail

revision = "0079_lockout_threshold"
down_revision = "0078_merge_0077_heads"

KEY = "auth.session_policy"
# The range this revision leaves behind, and the one it moves into.
_WAS = (1, 20)
_NOW = (3, 10)


def _whole(value: object) -> int | None:
    """An integral number, or None. Booleans and strings are not a threshold."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return None


def _into_range(threshold: int) -> int | None:
    low, high = _WAS
    if threshold < low or threshold > high:
        return None
    floor, ceiling = _NOW
    if floor <= threshold <= ceiling:
        return None
    return floor if threshold < floor else ceiling


def _next_version(document: dict) -> int:
    """The version ``PUT /api/session-policy`` would store: the current one plus one."""
    raw = document.get("version", 0)
    if isinstance(raw, bool):
        raise TypeError("session policy version is not a number")
    return int(raw) + 1


def _trail(conn, old: dict, new: dict, version: int, loosening: bool) -> None:
    if not trail.trail_ready(conn):
        return
    trail.append(
        conn,
        {
            trail.ACTION: "session_policy.changed",
            trail.ACTOR: trail.SYSTEM,
            "old": old,
            "new": new,
            "loosening": loosening,
            "version": version,
        },
    )


def upgrade() -> None:
    conn = op.get_bind()
    if (
        conn.execute(text("SELECT to_regclass('public.system_config')")).scalar()
        is None
    ):
        return
    row = conn.execute(
        text("SELECT value FROM public.system_config WHERE key = :key"),
        {"key": KEY},
    ).first()
    if row is None:
        return
    document = row[0]
    if isinstance(document, str):
        document = json.loads(document)
    if not isinstance(document, dict) or "lockout_threshold" not in document:
        return
    previous = _whole(document["lockout_threshold"])
    if previous is None:
        return
    moved = _into_range(previous)
    if moved is None:
        return
    version = _next_version(document)
    updated = {**document, "lockout_threshold": moved, "version": version}
    conn.execute(
        text("""
            UPDATE public.system_config
            SET value = CAST(:value AS jsonb)
            WHERE key = :key
            """),
        {"key": KEY, "value": json.dumps(updated)},
    )
    old = {k: v for k, v in document.items() if k != "version"}
    new = {k: v for k, v in updated.items() if k != "version"}
    _trail(conn, old, new, version, moved > previous)


def downgrade() -> None:
    raise NotImplementedError(
        "Vigil schema revisions only run forward; restore instead"
    )
