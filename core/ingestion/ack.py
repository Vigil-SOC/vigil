"""Per-item "stored" acknowledgement for the daemon's in-memory finding queue.

A producer attaches a future to the queue item, the processor settles it once
the row is stored (or given up on), and the producer advances its source
cursor / Kafka offset only after every item in its page came back stored.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

Pending = List[Tuple[str, "asyncio.Future[bool]"]]


def new_ack() -> "asyncio.Future[bool]":
    return asyncio.get_running_loop().create_future()


def settle_ack(ack: Optional["asyncio.Future[bool]"], stored: bool) -> None:
    """Resolve ``ack`` once; a missing, settled or cancelled future is a no-op."""
    if ack is not None and not ack.done():
        ack.set_result(stored)


async def wait_all(pending: Pending) -> bool:
    """True when every item of the page was stored."""
    results = await asyncio.gather(*(ack for _, ack in pending))
    return all(results)


def stored_keys(pending: Pending) -> List[str]:
    """Keys whose ack resolved True. Safe to call after a cancelled wait."""
    return [
        key
        for key, ack in pending
        if ack.done() and not ack.cancelled() and ack.result()
    ]
