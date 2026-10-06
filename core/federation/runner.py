"""Federation poller — manages per-adapter loops driven by federation_sources.

Spawned by :class:`daemon.poller.DataPoller` when the daemon starts. Owns one
asyncio task per *configured* adapter; each task polls when the global toggle
AND its row's ``enabled`` flag are both true. Tasks survive across enable/
disable transitions — they just no-op while disabled.

Design notes (locked decisions from MVP plan):

* No backfill on cold start — first run yields nothing, only finds events
  created after the source is enabled.
* No auto-disable on errors — backoff up to 8x interval, no row mutation
  beyond ``consecutive_errors`` and ``last_error``.
* Per-source severity floor honored at ingest time (filter before enqueue).
* "Poll now" is a Redis flag the loop checks each tick.
* Live config changes (enable, interval, min_severity) are picked up on the
  next tick — no daemon restart needed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from core.config import DEFAULT_REDIS_URL, get_settings
from core.federation import registry, store
from core.federation.seed import seed_federation_sources
from core.ingestion.dedup import RedisDedupSet
from core.time import utcnow

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _severity_passes(finding_sev: Optional[str], floor: Optional[str]) -> bool:
    if not floor:
        return True
    rank_finding = _SEVERITY_RANK.get((finding_sev or "").lower(), -1)
    rank_floor = _SEVERITY_RANK.get(floor.lower(), 0)
    return rank_finding >= rank_floor


class FederationRunner:
    """Top-level federation orchestrator hosted inside the data poller.

    Holds one asyncio task per registered adapter. The DataPoller owns the
    output queue we publish findings onto.
    """

    def __init__(self, output_queue: Optional[asyncio.Queue]) -> None:
        self._output_queue = output_queue
        self._adapter_tasks: Dict[str, asyncio.Task] = {}
        self._dedup: Dict[str, RedisDedupSet] = {}
        # Sources that are currently "polling" — used so a source toggled OFF
        # then ON quickly doesn't double-fire while the old task winds down.
        self._adapters: Dict[str, registry.FederationAdapter] = {}
        # Sources already warned about as unconfigured, so the warning is
        # logged on the transition rather than every tick.
        self._unconfigured_warned: set = set()
        # Stats for the metrics endpoint.
        self.stats: Dict[str, Any] = {"polls": 0, "findings": 0, "errors": 0}

    def set_output_queue(self, queue: asyncio.Queue) -> None:
        self._output_queue = queue

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Main entry point — called as an asyncio task by DataPoller."""
        # 1. Seed rows for adapters whose integration is configured.
        try:
            seed_federation_sources()
        except Exception as e:
            logger.warning("Federation seed failed: %s", e)

        # 2. Spawn one task per registered adapter (instantiated once, reused).
        for adapter in registry.list_adapters():
            self._adapters[adapter.name] = adapter
            self._dedup[adapter.name] = RedisDedupSet(f"federation:{adapter.name}")
            self._adapter_tasks[adapter.name] = asyncio.create_task(
                self._adapter_loop(adapter, shutdown_event)
            )

        if not self._adapter_tasks:
            # No adapters at all — wait for shutdown.
            await shutdown_event.wait()
            return

        try:
            await asyncio.gather(*self._adapter_tasks.values(), return_exceptions=True)
        except asyncio.CancelledError:
            pass

    def is_active_for(self, source_id: str) -> bool:
        """True if federation owns polling for ``source_id`` right now.

        Used by the legacy per-source loops in :mod:`daemon.poller` to decide
        whether to skip — when federation is on for a source, the legacy loop
        must back off so we don't double-pull. A source with no registered
        adapter (e.g. its import failed) is never ours, so the legacy loop
        keeps polling it. A store read error also reads as False.
        """
        if not registry.is_registered(source_id):
            return False
        if not store.is_globally_enabled():
            return False
        row = store.get_source(source_id)
        return bool(row and row.get("enabled"))

    # ------------------------------------------------------------------
    # Per-adapter loop
    # ------------------------------------------------------------------

    async def _adapter_loop(
        self,
        adapter: registry.FederationAdapter,
        shutdown_event: asyncio.Event,
    ) -> None:
        source_id = adapter.name
        logger.info("Federation adapter %s loop started", source_id)
        # Smallest sane sleep when waiting for global+per-source enable.
        idle_seconds = 5.0
        store_ok = True

        while not shutdown_event.is_set():
            # Re-read DB state on every tick. Cheap enough at MVP cadence.
            try:
                row = store.read_source(source_id) or {}
                global_on = bool(store.read_global_settings().get("enabled", False))
            except Exception as e:
                # Idle like "disabled", but say why — once per outage, not per tick.
                if store_ok:
                    logger.warning(
                        "Federation %s: store read failed, idling until it "
                        "recovers: %s",
                        source_id,
                        e,
                    )
                store_ok = False
                row, global_on = {}, False
            else:
                if not store_ok:
                    logger.info("Federation %s: store reads recovered", source_id)
                store_ok = True

            if not (global_on and row.get("enabled")):
                # Disabled (globally or per-source) — light sleep then re-check.
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=idle_seconds)
                    break
                except asyncio.TimeoutError:
                    continue

            interval = int(row.get("interval_seconds") or adapter.default_interval())
            errors = int(row.get("consecutive_errors") or 0)
            backoff_mult = min(2**errors, 8) if errors else 1
            sleep_for = max(interval * backoff_mult, 5)

            # Honor "poll now" — a redis flag the API sets when the user
            # clicks the button. We check it each tick rather than wait.
            if await self._consume_poll_now(source_id):
                sleep_for = 0

            await self._do_one_tick(adapter, row)

            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=sleep_for)
                break
            except asyncio.TimeoutError:
                continue

        logger.info("Federation adapter %s loop exited", source_id)

    async def _do_one_tick(
        self,
        adapter: registry.FederationAdapter,
        row: Dict[str, Any],
    ) -> None:
        source_id = adapter.name
        # Integration disabled under an enabled row: polling would record an
        # empty success. Skip so last_success_at stalls and the lag signal
        # marks the source quiet.
        if not adapter.is_configured():
            if source_id not in self._unconfigured_warned:
                self._unconfigured_warned.add(source_id)
                logger.warning(
                    "Federation %s is enabled but its integration is not "
                    "configured; skipping polls",
                    source_id,
                )
            return
        self._unconfigured_warned.discard(source_id)

        max_items = int(row.get("max_items") or 100)
        cursor = row.get("cursor") or {}
        min_severity = row.get("min_severity")

        self.stats["polls"] = self.stats.get("polls", 0) + 1
        try:
            result = await adapter.fetch(
                since=None,
                cursor=cursor if isinstance(cursor, dict) else {},
                max_items=max_items,
            )
        except Exception as e:
            logger.warning("Federation %s fetch raised: %s", source_id, e)
            self.stats["errors"] = self.stats.get("errors", 0) + 1
            store.record_failure(source_id, str(e))
            return

        candidates = []
        dropped = result.dropped
        for finding in result.findings:
            if not _severity_passes(finding.get("severity"), min_severity):
                continue
            ext = finding.get("external_id") or finding.get("finding_id")
            if ext:
                candidates.append((finding, ext))
            else:
                dropped += 1

        # One dedup round-trip to check the batch and one to mark it.
        enqueued: List[str] = []
        if candidates:
            dedup = self._dedup[source_id]
            seen = await dedup.are_processed(ext for _, ext in candidates)
            try:
                for finding, ext in candidates:
                    if ext in seen:
                        continue
                    # No queue means the put never happened — leave it unmarked.
                    if not await self._enqueue(finding, source_id, dedup, ext):
                        continue
                    # A repeat of this id later in the batch is a duplicate.
                    seen.add(ext)
                    enqueued.append(ext)
            finally:
                await dedup.mark_many(enqueued)
        new_count = len(enqueued)

        if new_count:
            self.stats["findings"] = self.stats.get("findings", 0) + new_count
            logger.info("Federation %s ingested %d finding(s)", source_id, new_count)

        if dropped:
            self.stats["dropped"] = self.stats.get("dropped", 0) + dropped
        store.record_success(source_id, cursor=result.cursor or {}, dropped=dropped)

    async def _enqueue(
        self,
        finding: Dict[str, Any],
        source_id: str,
        dedup: RedisDedupSet,
        dedup_key: str,
    ) -> bool:
        """True when the finding was put. False leaves it unmarked."""
        if self._output_queue is None:
            return False
        await self._output_queue.put(
            {
                "type": "finding",
                "source": source_id,
                "data": finding,
                "timestamp": utcnow().isoformat(),
                "dedup": dedup,
                "dedup_key": dedup_key,
            }
        )
        return True

    # ------------------------------------------------------------------
    # Poll-now bypass (Redis flag set by the API)
    # ------------------------------------------------------------------

    async def _consume_poll_now(self, source_id: str) -> bool:
        """Return True if the user clicked Poll Now since the last tick."""
        try:
            import redis.asyncio as aioredis  # type: ignore

            url = get_settings().redis_url or DEFAULT_REDIS_URL
            r = aioredis.from_url(url, decode_responses=True)
            key = f"vigil:federation:trigger:{source_id}"
            try:
                # GETDEL is atomic — flag is consumed on read.
                val = await r.getdel(key)
            finally:
                await r.aclose()
            return val is not None
        except Exception:
            return False


def request_poll_now(source_id: str) -> bool:
    """Set the Redis flag the runner consumes to trigger an immediate poll.

    Returns True if the flag was successfully set. Used by the backend API.
    Sync wrapper around redis-py's sync client so the FastAPI handler doesn't
    need its own event loop dance.
    """
    try:
        import redis  # type: ignore

        url = get_settings().redis_url or DEFAULT_REDIS_URL
        # The context manager closes the client's pool on every path.
        with redis.from_url(url, decode_responses=True) as client:
            client.set(
                f"vigil:federation:trigger:{source_id}",
                str(int(time.time())),
                ex=300,
            )
        return True
    except Exception as e:
        logger.warning("request_poll_now(%s) failed: %s", source_id, e)
        return False
