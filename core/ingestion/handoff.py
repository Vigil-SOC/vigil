"""Handing a finding to the daemon's processor over its bounded input queue."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from core.time import utcnow


def envelope(finding: Dict[str, Any], source: str) -> Dict[str, Any]:
    return {
        "type": "finding",
        "source": source,
        "data": finding,
        "timestamp": utcnow().isoformat(),
    }


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
