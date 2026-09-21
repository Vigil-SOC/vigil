"""Periodic poller for STIX/TAXII threat feeds (Cloudforce One et al).

Registered as a scheduled task by `daemon/scheduler.py` when the
`cloudforce_one` integration is enabled. No-op when disabled.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import get_settings
from core.time import utcnow

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
                _last_polled[key] = utcnow() - timedelta(seconds=60)
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
        summary["intake"] = self.offer_uncovered_indicators_to_intake()
        if total_seen or errors:
            logger.info("Threat feed poll: %s", summary)
        return summary

    def offer_uncovered_indicators_to_intake(self) -> Dict[str, Any]:
        """Offer each uncovered recent indicator as a case-less schedule row.

        Uses the same coverage check #905 uses. Does not open the hunt: the
        drain tick launches it. A queued intel row for the same entity_key is
        not inserted again.
        """
        try:
            from core.threat_intel.threat_feed_service import (
                propose_hunts_from_recent_indicators,
            )
            from services.daemon.orchestrator import insert_intake_trigger
        except Exception as e:  # noqa: BLE001
            logger.warning("intel intake producer unavailable: %s", e)
            return {"error": str(e)}

        try:
            result = propose_hunts_from_recent_indicators()
        except Exception as e:  # noqa: BLE001
            logger.warning("feed hunt proposals failed: %s", e)
            return {"error": str(e)}

        queued = _queued_intel_entity_keys()
        inserted = 0
        skipped_queued = 0
        for proposal in result.get("proposals") or []:
            entity_key = proposal.get("entity_key")
            if not entity_key:
                continue
            if entity_key in queued:
                skipped_queued += 1
                continue
            body = proposal.get("proposal") or {}
            trigger_id = insert_intake_trigger(
                kind="schedule",
                priority="low",
                payload={
                    "workflow_id": "threat-hunt",
                    "trigger_type": "intel",
                    "finding_ids": [],
                    "hypothesis": body.get("hypothesis"),
                    "hypothesis_subjects": body.get("hypothesis_subjects"),
                    "entity_key": entity_key,
                },
            )
            if trigger_id is None:
                skipped_queued += 1
                continue
            inserted += 1
            queued.add(entity_key)
        offered = {"inserted": inserted, "skipped_queued": skipped_queued}
        if inserted or skipped_queued:
            logger.info("Uncovered feed indicators offered to intake: %s", offered)
        return offered


def _queued_intel_entity_keys() -> set:
    """Entity keys already sitting on a queued intel schedule row."""
    try:
        from core.storage.connection import get_db_manager
        from core.storage.models import IntakeTrigger
    except Exception as e:  # noqa: BLE001
        logger.debug("intake read unavailable for intel dedup: %s", e)
        return set()
    try:
        with get_db_manager().session_scope() as session:
            rows = (
                session.query(IntakeTrigger)
                .filter(
                    IntakeTrigger.kind == "schedule",
                    IntakeTrigger.state == "queued",
                )
                .all()
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("could not read queued intel triggers: %s", e)
        return set()
    keys = set()
    for row in rows:
        payload = row.payload or {}
        if payload.get("trigger_type") != "intel":
            continue
        key = payload.get("entity_key")
        if key:
            keys.add(key)
    return keys
