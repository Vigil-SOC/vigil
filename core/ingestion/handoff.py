"""Handing a finding to the daemon's processor over its bounded input queue."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any, Dict, Optional

from core.ingestion.dedup import RedisDedupSet
from core.time import utcnow

logger = logging.getLogger(__name__)

# Producers that found the hand-off full and had to wait, by source.
full_waits: Dict[str, int] = defaultdict(int)
_full_counter: Any = None


def envelope(
    finding: Dict[str, Any],
    source: str,
    *,
    dedup: Optional[RedisDedupSet] = None,
    dedup_key: Optional[str] = None,
) -> Dict[str, Any]:
    # dedup/dedup_key: the key the producer marks, which the processor forgets
    # if it gives up on the store (#1341).
    return {
        "type": "finding",
        "source": source,
        "data": finding,
        "timestamp": utcnow().isoformat(),
        "dedup": dedup,
        "dedup_key": dedup_key,
    }


def _record_full(item: Any) -> None:
    global _full_counter
    source = str(item.get("source")) if isinstance(item, dict) else "unknown"
    full_waits[source] += 1
    try:
        # Created on first use so it binds to the meter init_telemetry set up.
        if _full_counter is None:
            from core.telemetry import get_meter

            _full_counter = get_meter("vigil.daemon").create_counter(
                "soc_daemon_handoff_full_total",
                description="Findings whose producer waited on a full hand-off",
                unit="1",
            )
        _full_counter.add(1, {"source": source})
    except Exception as e:
        logger.debug("handoff full counter unavailable: %s", e)


async def put_or_shutdown(
    queue: asyncio.Queue, item: Any, shutdown: asyncio.Event
) -> bool:
    """Put ``item``, waiting for room. False if shutdown came first.

    A full queue must not hold the daemon's shutdown until the processor
    drains, so the wait races the shutdown event. On False nothing was put:
    the caller keeps its bookmark or offset where it was and the source is
    re-read on the next boot.
    """
    if shutdown.is_set():
        return False
    try:
        queue.put_nowait(item)
        return True
    except asyncio.QueueFull:
        _record_full(item)
    put = asyncio.ensure_future(queue.put(item))
    stop = asyncio.ensure_future(shutdown.wait())
    try:
        await asyncio.wait({put, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.cancel()
        if not put.done():
            put.cancel()
    if put.done() and not put.cancelled():
        put.result()
        return True
    return False
