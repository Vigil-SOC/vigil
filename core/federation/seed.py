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
    """Switch Federation on, once, for an install that has never used it.

    "Used" is the global switch on, any row enabled, or any row with a
    ``last_poll_at`` (every attempted poll writes it). The global switch alone
    is not enough: an operator who set sources up and paused Federation has
    used it, and their per-source choices stand. An install that has not used
    it gets the global switch and every configured source switched on, each
    at the interval the legacy loop polled it at.

    The marker is written either way, in the same transaction, so a later
    deliberate switch-off survives restarts. Reads raise rather than default:
    a read that failed must not look like an install nobody has used.
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

        if not used:
            for adapter in list_adapters():
                try:
                    if not adapter.is_configured():
                        continue
                    interval = _legacy_interval(adapter.name)
                    row = rows.get(adapter.name)
                    if row is None:
                        row = FederationSource(
                            source_id=adapter.name, **_row_defaults(adapter)
                        )
                        session.add(row)
                    row.enabled = True
                    if interval:
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
        else:
            logger.info("Federation upgrade: already in use, left as configured")

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
