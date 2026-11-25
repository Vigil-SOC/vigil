"""Refill the two in-flight re-decision runs (#5093).

The readiness sweep only opens the next batch. Every API replica runs this
loop. The claim lock is what keeps one ticket to one run.
"""

from __future__ import annotations

import asyncio
import logging

from core.reporting.capped_recheck import drive

logger = logging.getLogger(__name__)

# The first pass waits for the schema. After that the slots refill quickly.
FIRST_SWEEP_SECONDS = 15
SWEEP_SECONDS = 5


async def run() -> None:
    await asyncio.sleep(FIRST_SWEEP_SECONDS)
    while True:
        try:
            await drive()
        except Exception:  # noqa: BLE001 - the next pass tries again
            logger.exception("capped re-check driver failed")
        await asyncio.sleep(SWEEP_SECONDS)
