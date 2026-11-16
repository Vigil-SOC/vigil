"""The sweep that scans operator skills no verdict covers yet.

Skills reach the folder by many routes the API never sees (copied in, cloned,
restored from a backup, there before the upgrade), so the sweep, not the upload
handler, is what guarantees a scan. It runs shortly after start and every few
minutes, and an upload or save wakes it at once. One replica sweeps at a time: a
session-level advisory lock is held for the whole run, since a scan can take
minutes and a transaction that long would be worse than a lock that lapses with
its connection.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy import text

from core.skills import skill_hold
from core.storage.connection import get_db_manager

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 300
# Startup does not wait on the schema; the first sweep comes after it settles.
FIRST_SWEEP_SECONDS = 60
# Advisory lock key ("skilSC") serializing sweeps across replicas.
LOCK = 0x736B_696C_5343

_wake: Optional[asyncio.Event] = None


def request() -> None:
    """Sweep now rather than at the next interval. Call from the event loop."""
    if _wake is not None:
        _wake.set()


async def run() -> None:
    global _wake
    _wake = asyncio.Event()
    # A previous shutdown in this process leaves it set (tests start the app twice).
    skill_hold.cancel.clear()
    await asyncio.sleep(FIRST_SWEEP_SECONDS)
    while True:
        _wake.clear()
        try:
            await asyncio.to_thread(sweep)
        except Exception:  # noqa: BLE001 - the next sweep tries again
            logger.exception("skill scan sweep failed")
        try:
            await asyncio.wait_for(_wake.wait(), timeout=SWEEP_SECONDS)
        except asyncio.TimeoutError:
            pass


def sweep() -> int:
    """One sweep under the lock; 0 when scanning is off or another replica holds it."""
    if not skill_hold.scanning_on():
        return 0
    manager = get_db_manager()
    if manager.engine is None:
        manager.initialize()
    conn = manager.engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        got = conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK}
        ).scalar()
        if not got:
            return 0
        try:
            return skill_hold.sweep()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK})
    finally:
        conn.close()
