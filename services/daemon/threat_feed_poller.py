"""Periodic poller for STIX/TAXII threat feeds.

Polls every enabled integration whose descriptor declares a ``taxii_feed``
and has collection ids saved; it names no vendor. Always registered as a
scheduled task by `daemon/scheduler.py`; each run is a no-op while no such
integration is enabled.

After the upsert loop it offers the poll's uncovered indicators to the Intake
as one case-less `kind="schedule"` row carrying every key (#1009). It does not
open the hunt; the drain tick launches it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple

from sqlalchemy import and_, or_

from core.auth.permissions import builtin_reader
from core.config import get_settings, is_integration_enabled
from core.integrations._base.config import resolve
from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    TaxiiFeed,
    iter_descriptors,
)
from core.integrations._base.proxy import integration_proxy
from core.memory.hunt_coverage import RETRY, build_proposal
from core.storage.connection import get_db_manager
from core.storage.intake_queue import insert_intake_trigger
from core.storage.models import IntakeTrigger
from core.storage.models.episodic import EpisodicCoverageEntry
from core.threat_intel import threat_feed_service as feed
from core.time import utcnow
from core.workflows.enablement import is_enabled
from core.workflows.routing import SCHEDULED_WORKFLOW

logger = logging.getLogger(__name__)


class _IntelIntake(NamedTuple):
    """What the intake already says about intel, read once per poll."""

    queued: bool
    spoken_for: Set[str]


# Track the last successful poll per (source, collection_id) so we only ask
# the TAXII server for objects added since then. Per-process, in-memory —
# good enough for a single daemon worker; restart causes a full re-pull,
# which is fine because indicator upserts are idempotent.
_last_polled: Dict[str, datetime] = {}

# Per source, what was missing from its feed the last time a poll skipped it,
# so the WARNING fires once per distinct state instead of every interval.
# Holds field names only, never the token.
_last_skip_signature: Dict[str, tuple] = {}


def _enabled_feed_integrations() -> List[IntegrationDescriptor]:
    """Enabled integrations whose descriptor declares a TAXII feed."""
    return [
        d
        for d in iter_descriptors()
        if d.taxii_feed is not None and is_integration_enabled(d.id)
    ]


def enabled_feeds() -> List[Tuple[str, TaxiiFeed]]:
    """(integration id, feed) for each enabled integration with collection ids."""
    out: List[Tuple[str, TaxiiFeed]] = []
    for d in _enabled_feed_integrations():
        try:
            declared = d.taxii_feed(resolve(d))  # type: ignore[misc]
        except Exception as e:  # noqa: BLE001 — one bad config skips one source
            logger.warning("%s threat feed config unreadable: %s", d.id, e)
            continue
        if declared is not None:
            out.append((d.id, declared))
    return out


class ThreatFeedPoller:
    """Pull STIX 2.1 indicators from every declared TAXII feed's collections."""

    @staticmethod
    def is_enabled() -> bool:
        return bool(_enabled_feed_integrations())

    @staticmethod
    def poll_interval_seconds() -> int:
        """Smallest saved interval across enabled feeds, then
        ``THREAT_FEED_POLL_INTERVAL``, then 900. Minimum 60."""
        try:
            saved = [
                f.poll_interval_seconds
                for _, f in enabled_feeds()
                if f.poll_interval_seconds is not None
            ]
            raw = min(saved) if saved else get_settings().threat_feed_poll_interval
            return max(60, int(raw))
        except Exception as e:  # noqa: BLE001
            logger.warning("Threat feed poll interval unreadable, using 900s: %s", e)
            return 900

    async def run_once(self) -> Dict[str, Any]:
        """Poll every enabled feed's collections; return per-source counters.

        The blocking fetch, upserts and intake offer run in a worker thread:
        the scheduler awaits this inline on the daemon's loop, so a long first
        pull must not stall it.
        """
        if not self.is_enabled():
            logger.debug("No threat feed integration enabled; skipping poll")
            return {"skipped": "integration_disabled"}
        return await asyncio.to_thread(self._poll)

    def _poll(self) -> Dict[str, Any]:
        sources: Dict[str, Any] = {}
        for source, declared in enabled_feeds():
            absent = tuple(
                name
                for name, present in (
                    ("api_token", declared.api_token),
                    ("server_url", declared.server_url),
                )
                if not present
            )
            if absent:
                self._note_skipped_config(source, absent)
                sources[source] = {"skipped": "incomplete_config"}
                continue
            _last_skip_signature.pop(source, None)
            try:
                sources[source] = _poll_feed(source, declared)
            except Exception as e:  # noqa: BLE001 — one source never stops another
                logger.error("Threat feed poll failed for %s: %s", source, e)
                sources[source] = {"error": str(e)}

        if not sources:
            return {"skipped": "no_collections"}
        summary: Dict[str, Any] = {"sources": sources}
        polled = [r for r in sources.values() if "totals" in r]
        if not polled:
            return summary
        # Once per poll, not per source, and every poll that ran, not only one
        # whose counters moved: the producer reads `threat_indicators`, not
        # this poll's results, so a poll that fetched nothing new can still be
        # the one that offers a key an earlier poll wrote and a refused insert
        # left behind.
        summary["intake"] = self.offer_uncovered_indicators_to_intake()
        if any(r["totals"]["seen"] or r["totals"]["errors"] for r in polled):
            logger.info("Threat feed poll: %s", summary)
        return summary

    @staticmethod
    def _note_skipped_config(source: str, absent: tuple) -> None:
        """Warn once per distinct missing set when a source skips for config."""
        if _last_skip_signature.get(source) == absent:
            logger.debug("%s still missing %s; skipping", source, ", ".join(absent))
            return
        _last_skip_signature[source] = absent
        logger.warning(
            "%s is enabled with collection ids but missing %s; its feed poll skipped",
            source,
            ", ".join(absent),
        )

    def offer_uncovered_indicators_to_intake(self) -> Dict[str, Any]:
        """Offer this poll's uncovered keys as one case-less schedule row.

        One row per poll, not one per key: up to 200 low-priority hunts against
        the hourly cost brake would let a feed decide when critical detections
        stop launching. The keys that did not make this row are still uncovered
        on the next poll and go into its row.

        Does not open the hunt: the drain tick launches it. One intel row is
        queued at a time, and keys a launched intel row named are skipped for
        `RETRY` after its launch. Coverage is read as Vigil (`builtin_reader`),
        so a hunt on a restricted Case still covers its keys. A covered key the
        proposal reopens gets a `reopen` coverage entry, written with the row.
        """
        if not is_enabled(SCHEDULED_WORKFLOW):
            return {"inserted": 0, "skipped": "workflow_disabled"}

        # Read before proposing, not after: a poll that cannot offer anything
        # should not spend a coverage check per recent indicator finding that out.
        intake = _intel_intake_state()
        if intake.queued:
            return {"inserted": 0, "skipped": "intel_row_queued"}

        try:
            with builtin_reader():
                result = feed.propose_hunts_from_recent_indicators()
        except Exception as e:  # noqa: BLE001
            logger.warning("feed hunt proposals failed: %s", e)
            return {"error": str(e)}

        keys: List[str] = []
        reopens: List[EpisodicCoverageEntry] = []
        skipped = 0
        now = datetime.now(timezone.utc)
        for proposal in result.get("proposals") or []:
            key = proposal["entity_key"]
            if key in intake.spoken_for:
                skipped += 1
                continue
            keys.append(key)
            if proposal.get("reopen"):
                reopens.append(_reopen_entry(key, proposal["reopen"], now))

        if not keys:
            return {"inserted": 0, "keys": 0, "skipped_recent": skipped}

        # One statement for the whole row, minted where the coverage proposal
        # mints its own: `kept_subjects` drops subjects whose statement text is
        # not the one being put up, so the hypothesis and the subjects key have
        # to be the same string.
        body = build_proposal(keys, [])
        try:
            # The row and its reopen entries commit together or not at all.
            with get_db_manager().session_scope() as session:
                insert_intake_trigger(
                    session=session,
                    kind="schedule",
                    priority="low",
                    payload={
                        "workflow_id": SCHEDULED_WORKFLOW,
                        "trigger_type": "intel",
                        "finding_ids": [],
                        "hypothesis": body["hypothesis"],
                        "hypothesis_subjects": body["hypothesis_subjects"],
                    },
                )
                session.add_all(reopens)
        except Exception as e:  # noqa: BLE001 — a refused insert is not a failed poll
            logger.warning("could not offer uncovered indicators to intake: %s", e)
            return {"error": str(e)}

        # No `None` to weigh: that is the queued-finding unique index answering,
        # and a schedule row carries no finding_id to collide on.
        offered = {
            "inserted": 1,
            "keys": len(keys),
            "reopened": len(reopens),
            "skipped_recent": skipped,
        }
        logger.info("Uncovered feed indicators offered to intake: %s", offered)
        return offered


def _reopen_entry(
    key: str, reopen: Dict[str, Any], now: datetime
) -> EpisodicCoverageEntry:
    """The `reopen` coverage entry for a key whose feed object changed (D5)."""
    check = reopen.get("postdates") or {}
    on = check.get("investigation_kind") or "?"
    if check.get("investigation_id"):
        on += f" {check['investigation_id']}"
    return EpisodicCoverageEntry(
        key=key,
        method="reopen",
        investigation_kind=None,
        investigation_id=None,
        outcome=None,
        trust="agent",
        concluded_at=now,
        reason=(
            f"{reopen.get('source')} object changed at {reopen.get('object_time')}, "
            f"after the {check.get('outcome') or check.get('method')} check on "
            f"{on} concluded at {check.get('concluded_at')}"
        ),
    )


def _intel_intake_state() -> _IntelIntake:
    """Whether an intel row is waiting, and which keys are not due a fresh look.

    One query for both, filtered on the payload rather than read back and
    sifted in Python. A row still `queued` counts however old it is: a poll
    while one waits neither restates its keys nor adds a second row. Past that,
    only a row that launched speaks for its keys, and its window runs from the
    launch (`decided_at`): a row the TTL expired never hunted anything.

    A read that fails answers "nothing is queued, nothing is spoken for". The
    insert is the guarded step, and holding every poll because the intake would
    not read would stop intel reaching the queue at all.
    """
    try:
        with get_db_manager().session_scope() as session:
            rows = (
                session.query(IntakeTrigger.state, IntakeTrigger.payload)
                .filter(
                    IntakeTrigger.kind == "schedule",
                    IntakeTrigger.payload["trigger_type"].astext == "intel",
                    or_(
                        IntakeTrigger.state == "queued",
                        and_(
                            IntakeTrigger.state == "launched",
                            IntakeTrigger.decided_at >= utcnow() - RETRY,
                        ),
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


def _poll_feed(source: str, declared: TaxiiFeed) -> Dict[str, Any]:
    """Fetch and upsert each collection of one feed; a failed one keeps its watermark."""
    proxy = integration_proxy(source)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    per_collection: Dict[str, Dict[str, Any]] = {}
    totals = {"seen": 0, "inserted": 0, "updated": 0, "skipped": 0, "errors": 0}
    for cid in declared.collection_ids:
        key = f"{source}::{cid}"
        since: Optional[datetime] = _last_polled.get(key)
        try:
            indicators = feed.fetch_taxii_collection(
                server_url=str(declared.server_url),
                collection_id=cid,
                api_token=str(declared.api_token),
                source=source,
                since=since,
                verify=declared.requests_verify,
                proxies=proxies,
            )
            counts = feed.upsert_indicators(indicators)
        except Exception as e:  # noqa: BLE001
            logger.error("%s poll failed for collection %s: %s", source, cid, e)
            totals["errors"] += 1
            per_collection[cid] = {"error": str(e)}
            continue
        per_collection[cid] = {"seen": len(indicators), **counts}
        totals["seen"] += len(indicators)
        for name in ("inserted", "updated", "skipped"):
            totals[name] += counts.get(name, 0)
        # Upserts are idempotent, so holding the watermark re-pulls the
        # rejected indicators next poll instead of losing them.
        if counts.get("skipped", 0) == 0:
            _last_polled[key] = utcnow() - timedelta(seconds=60)
        else:
            logger.warning(
                "%s collection %s: %d indicator(s) not stored; "
                "keeping watermark so the next poll re-pulls them",
                source,
                cid,
                counts["skipped"],
            )
    return {"collections": per_collection, "totals": totals}
