"""Breached passwords an administrator imported, and when the list was last refreshed (#4175).

The shipped list (``data/common_passwords.txt``) stays a file. An import adds
lowercased entries to the ``breached_passwords`` table, so every replica and a
backup sees them; nothing deletes one. The last refresh is one ``system_config``
row. A list whose later of shipped date and last import is over 90 days old is a
live warning to those who manage sessions and, once per stale period, a trail entry.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Optional

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from core.auth import trail_events
from core.auth.password_hash import MAX_PASSWORD_LENGTH
from core.platform import notices
from core.storage.models import BreachedPassword, SystemConfig, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

KEY = "auth.breached_list"
ACTION = "auth.breached_list_stale"
ACTOR = "system"
LOCK = 4175  # advisory lock: replicas take turns at the daily check
STALE_DAYS = 90
SETTINGS_LINK = "/settings?section=sessions"
# Larger than a full 1M-line SecLists file (about 9 MB), with room to spare.
MAX_BYTES = 64 * 1024 * 1024
MIN_LENGTH = 12  # no policy minimum lets a shorter entry through
BATCH = 5000

_NOTICE = Path(__file__).resolve().parent.parent.parent / "data" / "NOTICE"
# The same email-shaped test as the shipped list's filter (data/NOTICE).
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def shipped_date() -> Optional[date]:
    """The "Checked: YYYY-MM-DD" line of ``data/NOTICE``; None when it is missing."""
    try:
        found = re.search(
            r"^Checked: (\d{4}-\d{2}-\d{2})$", _NOTICE.read_text("utf-8"), re.M
        )
        return date.fromisoformat(found.group(1)) if found else None
    except (OSError, ValueError):
        return None


def _meta(session: Session) -> dict:
    row = session.get(SystemConfig, KEY)
    return dict(row.value or {}) if row else {}


def last_import(session: Session) -> Optional[datetime]:
    at = _meta(session).get("refreshed_at")
    try:
        return datetime.fromisoformat(at) if at else None
    except ValueError:
        return None


def effective_refresh(session: Session) -> Optional[datetime]:
    """The later of the shipped list's date and the last import (naive UTC)."""
    shipped = shipped_date()
    found = [datetime.combine(shipped, dtime.min)] if shipped else []
    if (imported := last_import(session)) is not None:
        found.append(imported)
    return max(found, default=None)


def is_imported(folded: str) -> bool:
    """Whether the lowercased ``folded`` is an imported entry (a primary-key hit)."""
    with unit_of_work() as session:
        return session.get(BreachedPassword, folded) is not None


def status(session: Session, shipped_count: int) -> dict:
    meta = _meta(session)
    effective = effective_refresh(session)
    return {
        "shipped_date": shipped_date(),
        "shipped_count": shipped_count,
        "imported_count": session.query(func.count(BreachedPassword.entry)).scalar(),
        "last_import_at": last_import(session),
        "last_import_sha256": meta.get("sha256"),
        "refreshed_at": effective,
        "stale": is_stale(effective),
    }


def is_stale(effective: Optional[datetime]) -> bool:
    return effective is None or utcnow() - effective > timedelta(days=STALE_DAYS)


def import_entries(session: Session, stream: BinaryIO, shipped: frozenset) -> dict:
    """Add the usable lines of ``stream`` (one entry per line, read as a stream).

    Skipped: blanks, entries under 12 characters or over the password maximum,
    email-shaped and undecodable lines. Counts what was read, added, already
    listed (shipped, imported before, or repeated in the file) and skipped; the
    file's SHA-256 goes on the trail with the counts, never the entries."""
    digest = hashlib.sha256()
    read = skipped = added = 0
    batch: dict[str, None] = {}

    def flush() -> None:
        nonlocal added
        fresh = [e for e in batch if e not in shipped]
        batch.clear()
        if fresh:
            # One array parameter and one statement per batch: far faster than a
            # bind parameter per entry.
            added += session.execute(
                text(
                    "INSERT INTO breached_passwords (entry) "
                    "SELECT unnest(CAST(:entries AS text[])) ON CONFLICT DO NOTHING"
                ),
                {"entries": fresh},
            ).rowcount

    for raw in stream:
        digest.update(raw)
        line = raw.rstrip(b"\r\n")
        if not line.strip():
            continue  # blanks are not entries
        read += 1
        try:
            entry = line.decode("utf-8")
        except UnicodeDecodeError:
            skipped += 1
            continue
        if (
            not MIN_LENGTH <= len(entry) <= MAX_PASSWORD_LENGTH
            or "\x00" in entry
            or _EMAIL.match(entry)
        ):
            skipped += 1
            continue
        batch[entry.lower()] = None
        if len(batch) >= BATCH:
            flush()
    flush()

    now = utcnow()
    sha256 = digest.hexdigest()
    value = {"refreshed_at": now.isoformat(), "sha256": sha256, "added": added}
    session.execute(
        insert(SystemConfig)
        .values(
            key=KEY,
            value=value,
            description="Breached-password list: last import",
            config_type="auth",
        )
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )
    return {
        "read": read,
        "added": added,
        "already_listed": read - skipped - added,
        "skipped": skipped,
        "sha256": sha256,
        "refreshed_at": now,
    }


def warning(user: User) -> list[dict[str, str]]:
    """The bell's condition, for a holder of ``sessions.manage``."""
    from core.auth.permissions import missing_permission

    if missing_permission(user.user_id, "sessions.manage") is not None:
        return []
    with unit_of_work() as session:
        effective = effective_refresh(session)
    if not is_stale(effective):
        return []
    since = f"last refreshed {effective:%Y-%m-%d}" if effective else "of unknown date"
    return [
        {
            "message": f"The breached-password list is over {STALE_DAYS} days old "
            f"({since}). Import a newer one.",
            "link": SETTINGS_LINK,
        }
    ]


def register() -> None:
    notices.CONDITIONS.append(warning)


def record_daily() -> bool:
    """Write "breached list stale" unless the trail has one since the last refresh.

    Safe on several replicas and on restarts: the lock serializes the check and
    the write. An import is its own trail entry, so a refresh needs none here."""
    with unit_of_work() as session:
        if not trail_events.ready(session):
            return False
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK})
        effective = effective_refresh(session)
        if not is_stale(effective):
            return False
        since = (effective or datetime.min).replace(tzinfo=timezone.utc)
        done = session.execute(
            text(
                "SELECT 1 FROM audit_trail WHERE payload->>'action' = :a "
                "AND occurred_at >= :since LIMIT 1"
            ),
            {"a": ACTION, "since": since},
        ).first()
        if done:
            return False
        trail_events.record(
            session,
            ACTION,
            actor=ACTOR,
            refreshed_at=effective.isoformat() if effective else None,
            days=(utcnow() - effective).days if effective else None,
        )
        return True
