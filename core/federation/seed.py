"""Auto-seed ``federation_sources`` rows on daemon boot.

For every adapter whose underlying integration is configured, ensure a row
exists with sensible defaults (default disabled — opt-in feature). Rows
already present are left untouched, so user edits in the Federation UI
survive restarts.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from core.federation.contract import FederationAdapter
from core.federation.registry import list_adapters
from core.federation.store import upsert_source

logger = logging.getLogger(__name__)


def seed_federation_sources(
    adapters: Optional[Iterable[FederationAdapter]] = None,
) -> List[str]:
    """Insert a row for each configured-but-unseen adapter (``adapters``, else all).

    Returns the list of source_ids touched (created or already-existing).
    Failures on individual sources are logged and skipped — a bad integration
    config can't break the rest of the seed pass.
    """
    seeded: List[str] = []
    for adapter in list_adapters() if adapters is None else adapters:
        try:
            if not adapter.is_configured():
                continue
            row = upsert_source(
                adapter.name, {"interval_seconds": adapter.default_interval()}
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
