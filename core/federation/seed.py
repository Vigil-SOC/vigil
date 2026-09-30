"""Federation's boot-time setup: the one-time switch-on, then row seeding.

For every adapter whose underlying integration is configured, ensure a row
exists with sensible defaults (switched on: Federation is the only polling
path). Rows already present are left untouched, so user edits in the
Federation UI survive restarts.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.config import get_settings
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


def apply_default_on() -> List[str]:
    """Switch Federation and every configured source on, once.

    Until Federation became the only polling path, a configured source was
    always polled: by Federation when the global switch and its row were both
    on, by its legacy loop otherwise. A disabled row or a paused global switch
    never meant "don't poll", so leaving them as they are would stop sources
    that were being polled. A row already on is left as it is. A row switched
    on here takes the interval its legacy loop polled at, unless Federation
    has polled it before (``last_poll_at``), in which case it keeps its own.

    The marker is written in the same transaction, so a later deliberate
    switch-off survives restarts. Reads raise rather than default: a failed
    read of the marker must not look like an upgrade that never ran.
    Returns the source ids switched on.
    """
    from core.storage.connection import get_db_manager
    from core.storage.models import FederationSource, SystemConfig

    switched_on: List[str] = []
    with get_db_manager().session_scope() as session:
        if session.get(SystemConfig, DEFAULT_ON_KEY) is not None:
            return []

        global_row = session.get(SystemConfig, GLOBAL_KEY)
        global_value = dict(global_row.value or {}) if global_row else {}
        rows = {r.source_id: r for r in session.query(FederationSource).all()}
        used = bool(global_value.get("enabled")) or any(
            r.enabled or r.last_poll_at is not None for r in rows.values()
        )

        for adapter in list_adapters():
            try:
                if not adapter.is_configured():
                    continue
                row = rows.get(adapter.name)
                if row is None:
                    row = FederationSource(
                        source_id=adapter.name, **_row_defaults(adapter)
                    )
                    session.add(row)
                elif row.enabled:
                    continue
                row.enabled = True
                interval = _legacy_interval(adapter.name)
                if interval and row.last_poll_at is None:
                    row.interval_seconds = interval
                switched_on.append(adapter.name)
                logger.info(
                    "Federation upgrade: switched on %s (interval %ss)",
                    adapter.name,
                    row.interval_seconds,
                )
            except Exception as e:
                logger.warning(
                    "Federation upgrade skipped %s: %s",
                    getattr(adapter, "name", "?"),
                    e,
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
                    )
                )
            else:
                global_row.value = global_value
            logger.info("Federation upgrade: switched Federation on")

        session.add(
            SystemConfig(
                key=DEFAULT_ON_KEY,
                value={
                    "applied_at": utcnow().isoformat(),
                    "switched_on": switched_on,
                    "already_in_use": used,
                },
                description="Federation's one-time switch-on has run",
                config_type="federation",
            )
        )
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
