"""Periodic poller for STIX/TAXII threat feeds (Cloudforce One et al).

Registered as a scheduled task by `daemon/scheduler.py` when the
`cloudforce_one` integration is enabled. No-op when disabled.

After the upsert loop it offers the poll's uncovered indicators to the Intake
as one case-less `kind="schedule"` row carrying every key (#1009). It does not
open the hunt; the drain tick launches it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, NamedTuple, Optional, Set

from core.config import get_settings
from core.time import utcnow

logger = logging.getLogger(__name__)

# How long a key an intel row already named stays spoken for. Coverage cannot
# answer this: `check_coverage` reads `workflow_runs.trigger_context` for the
# in-flight arm, and a hunt the orchestrator launches never gets a
# `workflow_runs` row (it enqueues the job directly and only
# `agent_runs_router` and `WorkflowsService.execute_workflow` call
# `begin_run`), while the concluded arm reads `episodic_verdicts` and a hunt
# that gathered nothing writes an `episodic_gaps` row instead. So a hunted key
# reads `uncovered` again on the next poll, and without this window the poller
# would re-launch the same hunt every interval forever. A constant, not a
# settings field, for the reason #905 gives about its own cap.
INTEL_RECHECK_AFTER = timedelta(days=7)


class _IntelIntake(NamedTuple):
    """What the intake already says about intel, read once per poll."""

    queued: bool
    spoken_for: Set[str]


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
        # Every poll that ran, not only one whose counters moved: the producer
        # reads `threat_indicators`, not this poll's results, so a poll that
        # fetched nothing new can still be the one that offers a key an earlier
        # poll wrote and a refused insert left behind.
        summary["intake"] = self.offer_uncovered_indicators_to_intake()
        if total_seen or errors:
            logger.info("Threat feed poll: %s", summary)
        return summary

    def offer_uncovered_indicators_to_intake(self) -> Dict[str, Any]:
        """Offer this poll's uncovered keys as one case-less schedule row.

        One row per poll, not one per key: up to 200 low-priority hunts against
        the hourly cost brake would let a feed decide when critical detections
        stop launching. The keys that did not make this row are still uncovered
        on the next poll and go into its row.

        Does not open the hunt: the drain tick launches it. One intel row is
        queued at a time, and keys an intel row already named are skipped for
        `INTEL_RECHECK_AFTER`.
        """
        try:
            from core.memory.hunt_coverage import build_proposal
            from core.threat_intel.threat_feed_service import (
                propose_hunts_from_recent_indicators,
            )
            from services.daemon.orchestrator import insert_intake_trigger
        except Exception as e:  # noqa: BLE001
            logger.warning("intel intake producer unavailable: %s", e)
            return {"error": str(e)}

        # Read before proposing, not after: a poll that cannot offer anything
        # should not spend a coverage check per recent indicator finding that out.
        intake = _intel_intake_state()
        if intake.queued:
            return {"inserted": 0, "skipped": "intel_row_queued"}

        try:
            result = propose_hunts_from_recent_indicators()
        except Exception as e:  # noqa: BLE001
            logger.warning("feed hunt proposals failed: %s", e)
            return {"error": str(e)}

        keys: List[str] = []
        skipped = 0
        for proposal in result.get("proposals") or []:
            key = proposal.get("entity_key")
            if not key or key in keys:
                continue
            if key in intake.spoken_for:
                skipped += 1
                continue
            keys.append(key)

        if not keys:
            return {"inserted": 0, "keys": 0, "skipped_recent": skipped}

        # One statement for the whole row, minted where the coverage proposal
        # mints its own: `kept_subjects` drops subjects whose statement text is
        # not the one being put up, so the hypothesis and the subjects key have
        # to be the same string.
        body = build_proposal(keys, [])
        try:
            insert_intake_trigger(
                kind="schedule",
                priority="low",
                payload={
                    "workflow_id": "threat-hunt",
                    "trigger_type": "intel",
                    "finding_ids": [],
                    "hypothesis": body["hypothesis"],
                    "hypothesis_subjects": body["hypothesis_subjects"],
                },
            )
        except Exception as e:  # noqa: BLE001 — a refused insert is not a failed poll
            logger.warning("could not offer uncovered indicators to intake: %s", e)
            return {"error": str(e)}

        # No `None` to weigh: that is the queued-finding unique index answering,
        # and a schedule row carries no finding_id to collide on.
        offered = {"inserted": 1, "keys": len(keys), "skipped_recent": skipped}
        logger.info("Uncovered feed indicators offered to intake: %s", offered)
        return offered


def _intel_intake_state() -> _IntelIntake:
    """Whether an intel row is waiting, and which keys are not due a fresh look.

    One query for both, filtered on the payload rather than read back and
    sifted in Python. A row still `queued` counts however old it is: a poll
    while one waits neither restates its keys nor adds a second row.

    A read that fails answers "nothing is queued, nothing is spoken for". The
    insert is the guarded step, and holding every poll because the intake would
    not read would stop intel reaching the queue at all.
    """
    try:
        from sqlalchemy import or_

        from core.storage.connection import get_db_manager
        from core.storage.models import IntakeTrigger
    except Exception as e:  # noqa: BLE001
        logger.debug("intake read unavailable for intel dedup: %s", e)
        return _IntelIntake(False, set())
    try:
        with get_db_manager().session_scope() as session:
            rows = (
                session.query(IntakeTrigger.state, IntakeTrigger.payload)
                .filter(
                    IntakeTrigger.kind == "schedule",
                    IntakeTrigger.payload["trigger_type"].astext == "intel",
                    or_(
                        IntakeTrigger.state == "queued",
                        IntakeTrigger.created_at >= utcnow() - INTEL_RECHECK_AFTER,
                    ),
                )
                .all()
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("could not read prior intel triggers: %s", e)
        return _IntelIntake(False, set())
    keys: Set[str] = set()
    queued = False
    for state, payload in rows:
        queued = queued or state == "queued"
        declared = (payload or {}).get("hypothesis_subjects")
        if not isinstance(declared, dict):
            continue
        for subjects in declared.values():
            if isinstance(subjects, list):
                keys.update(key for key in subjects if isinstance(key, str) and key)
    return _IntelIntake(queued, keys)
