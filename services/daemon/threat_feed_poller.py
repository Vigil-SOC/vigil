"""Periodic poller for STIX/TAXII threat feeds (Cloudforce One et al).

Registered as a scheduled task by `daemon/scheduler.py` when the
`cloudforce_one` integration is enabled. No-op when disabled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import get_settings

logger = logging.getLogger(__name__)


# Track the last successful poll per (source, collection_id) so we only ask
# the TAXII server for objects added since then. Per-process, in-memory —
# good enough for a single daemon worker; restart causes a full re-pull,
# which is fine because indicator upserts are idempotent.
_last_polled: Dict[str, datetime] = {}


class ThreatFeedPoller:
    """Pull STIX 2.1 indicators from configured TAXII collections."""

    def __init__(self) -> None:
        self.stats = {
            "runs": 0,
            "indicators_seen": 0,
            "inserted": 0,
            "updated": 0,
            "errors": 0,
        }

    @staticmethod
    def is_enabled() -> bool:
        try:
            from core.config import is_integration_enabled
        except Exception:  # noqa: BLE001
            return False
        return is_integration_enabled("cloudforce_one")

    @staticmethod
    def poll_interval_seconds() -> int:
        """Effective poll interval. Honors integration config and env override."""

        try:
            from core.config import get_integration_config

            cfg = get_integration_config("cloudforce_one") or {}
            raw = cfg.get("poll_interval_seconds")
        except Exception:  # noqa: BLE001
            raw = None

        if raw is None:
            raw = get_settings().threat_feed_poll_interval
        try:
            return max(60, int(raw))
        except (TypeError, ValueError):
            return 900

    async def run_once(self) -> Dict[str, Any]:
        """Poll all configured collections; return per-source counters."""
        if not self.is_enabled():
            logger.debug("Cloudforce One integration disabled; skipping poll")
            return {"skipped": "integration_disabled"}

        try:
            from core.config import get_integration_config
            from core.threat_intel import threat_feed_service as feed
        except Exception as e:  # noqa: BLE001
            logger.warning("Threat feed dependencies unavailable: %s", e)
            return {"error": str(e)}

        cfg = get_integration_config("cloudforce_one") or {}
        api_token = cfg.get("api_token")
        server_url = cfg.get("taxii_server_url")
        collection_ids_raw = cfg.get("collection_ids") or ""

        if not api_token or not server_url or not collection_ids_raw:
            logger.info(
                "Cloudforce One configured but missing token/url/collections; skipping"
            )
            return {"skipped": "incomplete_config"}

        collection_ids: List[str] = [
            c.strip() for c in str(collection_ids_raw).split(",") if c.strip()
        ]
        if not collection_ids:
            return {"skipped": "no_collections"}

        per_collection: Dict[str, Dict[str, int]] = {}
        total_seen = 0
        total_inserted = 0
        total_updated = 0
        errors = 0

        for cid in collection_ids:
            key = f"cloudforce_one::{cid}"
            since: Optional[datetime] = _last_polled.get(key)
            try:
                indicators = feed.fetch_taxii_collection(
                    server_url=server_url,
                    collection_id=cid,
                    api_token=api_token,
                    source="cloudforce_one",
                    since=since,
                )
                counts = feed.upsert_indicators(indicators)
                per_collection[cid] = {"seen": len(indicators), **counts}
                total_seen += len(indicators)
                total_inserted += counts.get("inserted", 0)
                total_updated += counts.get("updated", 0)
                _last_polled[key] = datetime.utcnow() - timedelta(seconds=60)
            except Exception as e:  # noqa: BLE001
                logger.error("Cloudforce One poll failed for %s: %s", cid, e)
                errors += 1
                per_collection[cid] = {"error": str(e)}

        self.stats["runs"] += 1
        self.stats["indicators_seen"] += total_seen
        self.stats["inserted"] += total_inserted
        self.stats["updated"] += total_updated
        self.stats["errors"] += errors

        summary = {
            "source": "cloudforce_one",
            "collections": per_collection,
            "totals": {
                "seen": total_seen,
                "inserted": total_inserted,
                "updated": total_updated,
                "errors": errors,
            },
        }
        if total_seen or errors:
            logger.info("Threat feed poll: %s", summary)
        return summary
