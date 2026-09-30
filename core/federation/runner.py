"""Federation poller — manages per-adapter loops driven by federation_sources.

Spawned by :class:`daemon.poller.DataPoller` when the daemon starts; it is the
only path that polls a source. Owns one asyncio task per *configured* adapter;
each task polls when the global toggle AND its row's ``enabled`` flag are both
true. Tasks survive across enable/disable transitions — they just no-op while
disabled.

Design notes (locked decisions from MVP plan):

* On by default: the first boot switches Federation and every configured
  source on, once (:func:`core.federation.seed.apply_default_on`).
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
from typing import Any, Dict, Optional

from core.config import DEFAULT_REDIS_URL, get_settings
from core.federation import registry, store
from core.federation.seed import apply_default_on, seed_federation_sources
from core.ingestion.dedup import RedisDedupSet
from core.ingestion.handoff import envelope, put_or_shutdown
from core.time import utcnow

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# Not zero: the hand-off to the processor has no size limit yet, so a source
# catching up must not outrun it.
_CATCH_UP_SECONDS = 5

_lag_gauge_registered = False


def _register_lag_gauge() -> None:
    global _lag_gauge_registered
    if _lag_gauge_registered:
        return
    _lag_gauge_registered = True
    try:
        from opentelemetry.metrics import Observation

        from core.telemetry import get_meter

        def _observe(_options: Any):
            try:
                enabled = store.is_globally_enabled()
                now = utcnow()
                observations = []
                for row in store.list_sources():
                    lag = store.source_lag_seconds(row, global_enabled=enabled, now=now)
                    if lag is not None:
                        observations.append(
                            Observation(lag, {"source_id": row["source_id"]})
                        )
                return observations
            except Exception as e:
                logger.debug("federation lag observation failed: %s", e)
                return []

        get_meter("vigil.federation").create_observable_gauge(
            "soc_daemon_federation_source_lag_seconds",
            callbacks=[_observe],
            description="How far behind each polling federation source Vigil is",
            unit="s",
        )
    except Exception as e:
        logger.debug("federation lag gauge not registered: %s", e)


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
        self._shutdown = asyncio.Event()
        self._adapter_tasks: Dict[str, asyncio.Task] = {}
        self._dedup: Dict[str, RedisDedupSet] = {}
        # Sources that are currently "polling" — used so a source toggled OFF
        # then ON quickly doesn't double-fire while the old task winds down.
        self._adapters: Dict[str, registry.FederationAdapter] = {}
        # Stats for the metrics endpoint.
        self.stats: Dict[str, Any] = {"polls": 0, "findings": 0, "errors": 0}

    def set_output_queue(self, queue: asyncio.Queue) -> None:
        self._output_queue = queue

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, shutdown_event: asyncio.Event) -> None:
        """Main entry point — called as an asyncio task by DataPoller."""
        self._shutdown = shutdown_event
        # 1. Switch Federation on the first time, then seed rows for adapters
        # whose integration is configured. The switch-on reads "has anyone
        # used Federation" from the rows, so it must run before seeding
        # creates any.
        try:
            apply_default_on()
        except Exception as e:
            logger.warning("Federation default-on upgrade failed: %s", e)
        try:
            seed_federation_sources()
        except Exception as e:
            logger.warning("Federation seed failed: %s", e)

        _register_lag_gauge()

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

        while not shutdown_event.is_set():
            # Re-read DB state on every tick. Cheap enough at MVP cadence.
            row = store.get_source(source_id) or {}
            global_on = store.is_globally_enabled()

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

            if await self._do_one_tick(adapter, row):
                sleep_for = min(sleep_for, _CATCH_UP_SECONDS)

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
    ) -> bool:
        """Fetch once and hand off what is new. True when the source may hold more."""
        source_id = adapter.name
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
            return False

        new_count = 0
        for finding in result.findings:
            if not _severity_passes(finding.get("severity"), min_severity):
                continue
            ext = finding.get("external_id") or finding.get("finding_id")
            if not ext:
                continue
            dedup = self._dedup[source_id]
            if await dedup.is_processed(ext):
                continue
            if not await self._enqueue(finding, source_id):
                # Shutdown while the hand-off was full: drop the rest of the
                # page and keep the cursor, so the next boot fetches it again.
                logger.info(
                    "Federation %s: shutdown during hand-off; cursor kept", source_id
                )
                return False
            await dedup.mark_processed(ext)
            new_count += 1

        if new_count:
            self.stats["findings"] = self.stats.get("findings", 0) + new_count
            logger.info("Federation %s ingested %d finding(s)", source_id, new_count)

        store.record_success(source_id, cursor=result.cursor or {})
        return result.truncated

    async def _enqueue(self, finding: Dict[str, Any], source_id: str) -> bool:
        """Wait for room in the hand-off. False if shutdown came first."""
        if self._output_queue is None:
            return True
        return await put_or_shutdown(
            self._output_queue, envelope(finding, source_id), self._shutdown
        )

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
            # GETDEL is atomic — flag is consumed on read.
            val = await r.getdel(key)
            await r.close()
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
        client = redis.from_url(url, decode_responses=True)
        client.set(
            f"vigil:federation:trigger:{source_id}", str(int(time.time())), ex=300
        )
        return True
    except Exception as e:
        logger.warning("request_poll_now(%s) failed: %s", source_id, e)
        return False
