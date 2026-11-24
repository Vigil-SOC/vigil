"""Reads OpenCTI's own facts onto its Integration Health record every 300 s (#5071).

Version, newest intelligence and connector backlog, while the ``opencti``
integration is switched on. Runs on every replica; a transaction advisory lock
held for the whole pass, and a claim read under it (the last read, kept or
failed, is under an interval old), make one replica read per interval; a
failed lookup counts as a failed read there. A success writes ``facts`` and
stamps the record as a lookup does; a failed read leaves ``facts`` and adds one
to the record's failure run.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta

from sqlalchemy import text

from core.federation.error_kind import kind_of
from core.storage import source_health
from core.storage.models import IntegrationConfig, SourceHealth
from core.storage.schemas.base import _iso_utc
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

SOURCE = "opencti"
# Overridable for a live check; the console judges staleness from the stored value.
INTERVAL_SECONDS = int(os.getenv("OPENCTI_FACTS_SECONDS", "300"))
FIRST_READ_SECONDS = 60
# Advisory lock key. Distinct from the readiness sweep's (4874).
LOCK = 5071
_TIMEOUT = 15.0


async def run() -> None:
    await asyncio.sleep(FIRST_READ_SECONDS)
    while True:
        try:
            await asyncio.to_thread(tick)
        except Exception:  # noqa: BLE001 - the next pass tries again
            logger.exception("OpenCTI facts read failed to run")
        await asyncio.sleep(INTERVAL_SECONDS)


def tick() -> None:
    # A worker thread: the lock's transaction stays open across the read and the write.
    with unit_of_work() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK})
        if not _due(session):
            return
        asyncio.run(_read())


def _due(session) -> bool:
    config = session.get(IntegrationConfig, SOURCE)
    if config is None or not config.enabled:
        return False
    row = session.get(SourceHealth, SOURCE)
    if row is None:
        return True
    reads = []
    if row.facts and row.facts.get("collected_at"):
        reads.append(
            datetime.fromisoformat(row.facts["collected_at"]).replace(tzinfo=None)
        )
    # A failed read keeps the facts, so its time is the failed attempt's.
    if row.last_attempt_ok is False and row.last_attempt_at is not None:
        reads.append(row.last_attempt_at)
    return not reads or utcnow() - max(reads) >= timedelta(
        seconds=INTERVAL_SECONDS * 0.9
    )


async def _read() -> None:
    from core.integrations.opencti.client import OpenCTIError, configured_service

    service, _ = configured_service(timeout=_TIMEOUT)
    if service is None:
        return  # not configured yet: nothing to read, and not a failure
    started = utcnow()
    try:
        facts = await service.read_facts()
    except OpenCTIError as exc:
        # Settings saved mid-read reset the record: this failure is the old settings'.
        if not _reset_since(started):
            source_health.record_failed(SOURCE, str(exc), kind_of(exc))
        return
    finally:
        await service.close()
    facts.update(collected_at=_iso_utc(utcnow()), interval_seconds=INTERVAL_SECONDS)
    source_health.record_facts(SOURCE, facts, started=started)


def _reset_since(started: datetime) -> bool:
    record = source_health.read(SOURCE) or {}
    reset_at = record.get("reset_at")
    return reset_at is not None and reset_at >= started
