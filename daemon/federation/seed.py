"""Auto-seed ``federation_sources`` rows on daemon boot.

For every adapter whose underlying integration is configured, ensure a row
exists with sensible defaults (default disabled — opt-in feature). Rows
already present are left untouched, so user edits in the Federation UI
survive restarts.
"""

from __future__ import annotations

import logging
from typing import List

from daemon.federation.registry import list_adapters
from daemon.federation.store import upsert_source

logger = logging.getLogger(__name__)


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
            row = upsert_source(
                adapter.name,
                {
                    "enabled": False,
                    "interval_seconds": adapter.default_interval(),
                    "max_items": 100,
                    "min_severity": None,
                    "cursor": {},
                    "consecutive_errors": 0,
                },
            )
            if row:
                seeded.append(adapter.name)
        except Exception as e:
            logger.warning(
                "Federation seed failed for %s: %s", getattr(adapter, "name", "?"), e
            )
    if seeded:
        logger.info("Federation seeded %d source(s): %s", len(seeded), seeded)
    return seeded
