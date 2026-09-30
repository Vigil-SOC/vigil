"""Federation's boot-time setup: the one-time switch-on, then row seeding.

For every adapter whose underlying integration is configured, ensure a row
exists with sensible defaults (switched on: Federation is the only polling
path). Rows already present are left untouched, so user edits in the
Federation UI survive restarts.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from core.config import get_settings
from core.federation.adapters._base import cursor_at
from core.federation.registry import list_adapters
from core.federation.store import GLOBAL_KEY, upsert_source
from core.time import utcnow

logger = logging.getLogger(__name__)

DEFAULT_ON_KEY = "federation.default_on_applied"

# The legacy loops polled these at the Splunk interval, CrowdStrike at its own.
_SPLUNK_CADENCE = {
    "splunk",
    "azure_sentinel",
    "aws_security_hub",
    "microsoft_defender",
    "elastic",
}

# How far back a source switched on at the upgrade may start reading.
_CATCH_UP_LIMIT = timedelta(hours=1)

_UPGRADE_BY = "system"
_UPGRADE_REASON = "Federation default-on upgrade"


def _row_defaults(adapter: Any) -> Dict[str, Any]:
    return {
        "enabled": True,
        "interval_seconds": adapter.default_interval(),
        "max_items": 100,
        "min_severity": None,
        "cursor": {},
        "consecutive_errors": 0,
    }


def _legacy_interval(source_id: str) -> Optional[int]:
    settings = get_settings()
    if source_id == "crowdstrike":
        return settings.daemon_crowdstrike_poll_interval
    if source_id in _SPLUNK_CADENCE:
        return settings.daemon_splunk_poll_interval
    return None


def _audit(session: Any, key: str, action: str, old: Any, new: Any) -> None:
    """Record a config change in the upgrade's own transaction, as ConfigService does."""
    from core.storage.models import ConfigAuditLog

    session.add(
        ConfigAuditLog(
            config_type="federation",
            config_key=key,
            action=action,
            old_value=old,
            new_value=new,
            changed_by=_UPGRADE_BY,
            change_reason=_UPGRADE_REASON,
        )
    )


def _catch_up_cursor(session: Any, source_id: str, now: datetime) -> Dict[str, Any]:
    """Resume where the legacy loop stopped: its newest stored alert.

    Empty (a cold start) when nothing is stored, and never earlier than
    ``_CATCH_UP_LIMIT`` ago. In a savepoint, so a missing ``findings`` table
    on a fresh install does not abort the upgrade's transaction.
    """
    from core.storage.models import Finding

    try:
        with session.begin_nested():
            newest = (
                session.query(func.max(Finding.timestamp))
                .filter(Finding.data_source == source_id)
                .scalar()
            )
    except Exception as e:
        logger.info("Federation upgrade: no stored findings for %s: %s", source_id, e)
        return {}
    if newest is None:
        return {}
    return cursor_at(max(min(newest, now), now - _CATCH_UP_LIMIT))


def apply_default_on() -> List[str]:
    """Switch Federation and every configured source on, once.

    Until Federation became the only polling path, a configured source was
    always polled: by Federation when the global switch and its row were both
    on, by its legacy loop otherwise. A disabled row or a paused global switch
    never meant "don't poll", so leaving them as they are would stop sources
    that were being polled. A row already on is left as it is. A row switched
    on here takes the interval its legacy loop polled at, unless Federation
    has polled it before (``last_poll_at``), in which case it keeps its own.
    Its cursor resumes from the newest alert the legacy loop stored, so the
    alerts between the last legacy poll and this boot are still read; any
    cursor it held is from before the legacy loop took over, and is behind.

    The marker, and an audit entry for it and for the global switch, are
    written in the same transaction, so a later deliberate switch-off
    survives restarts. Reads raise rather than default: a failed read of the
    marker must not look like an upgrade that never ran.
    Any failure raises and writes nothing, so setup retries rather than
    marking the upgrade done over a source it skipped.
    Returns the source ids switched on.
    """
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource, SystemConfig

    switched_on: List[str] = []
    now = utcnow()
    with get_db_manager().session_scope() as session:
        if session.get(SystemConfig, DEFAULT_ON_KEY) is not None:
            return []

        global_row = session.get(SystemConfig, GLOBAL_KEY)
        global_value = dict(global_row.value or {}) if global_row else {}
        global_before = dict(global_value) if global_row else None
        rows = {r.source_id: r for r in session.query(FederationSource).all()}
        used = bool(global_value.get("enabled")) or any(
            r.enabled or r.last_poll_at is not None for r in rows.values()
        )

        # Seeding never revisits a row, so one left off here stays off for good.
        to_switch = [r for r in rows.values() if not r.enabled]
        for adapter in list_adapters():
            if adapter.name not in rows and adapter.is_configured():
                row = FederationSource(source_id=adapter.name, **_row_defaults(adapter))
                session.add(row)
                to_switch.append(row)

        for row in to_switch:
            row.enabled = True
            interval = _legacy_interval(row.source_id)
            if interval and row.last_poll_at is None:
                row.interval_seconds = interval
            row.cursor = _catch_up_cursor(session, row.source_id, now)
            switched_on.append(row.source_id)
            logger.info(
                "Federation upgrade: switched on %s (interval %ss, from %s)",
                row.source_id,
                row.interval_seconds,
                row.cursor.get("last_poll_at") or "now",
            )

        if not global_value.get("enabled"):
            global_value["enabled"] = True
            if global_row is None:
                session.add(
                    SystemConfig(
                        key=GLOBAL_KEY,
                        value=global_value,
                        description="Federated monitoring global on/off",
                        config_type="federation",
                        updated_by=_UPGRADE_BY,
                    )
                )
            else:
                global_row.value = global_value
                global_row.updated_by = _UPGRADE_BY
            _audit(
                session,
                GLOBAL_KEY,
                "create" if global_row is None else "update",
                global_before,
                global_value,
            )
            logger.info("Federation upgrade: switched Federation on")

        marker = {
            "applied_at": utcnow().isoformat(),
            "switched_on": switched_on,
            "already_in_use": used,
        }
        session.add(
            SystemConfig(
                key=DEFAULT_ON_KEY,
                value=marker,
                description="Federation's one-time switch-on has run",
                config_type="federation",
                updated_by=_UPGRADE_BY,
            )
        )
        _audit(session, DEFAULT_ON_KEY, "create", None, marker)
    return switched_on


def seed_federation_sources() -> List[str]:
    """Insert a row for each configured-but-unseen adapter.

    Returns the list of source_ids touched (created or already-existing).
    Failures on individual sources are logged and skipped — a bad integration
    config can't break the rest of the seed pass.
    """
    seeded: List[str] = []
    for adapter in list_adapters():
        try:
            if not adapter.is_configured():
                continue
            row = upsert_source(adapter.name, _row_defaults(adapter))
            if row:
                seeded.append(adapter.name)
        except Exception as e:
            logger.warning(
                "Federation seed failed for %s: %s", getattr(adapter, "name", "?"), e
            )
    if seeded:
        logger.info("Federation seeded %d source(s): %s", len(seeded), seeded)
    return seeded
