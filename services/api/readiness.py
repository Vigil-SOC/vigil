"""The hourly sweep that records Install Readiness and re-checks one integration (#4874).

Runs on every replica, like the dormancy sweep. An advisory lock makes the
audit decision and the re-check claim once: a second replica, or a restart,
writes nothing when readiness is unchanged, and does not check the same
integration twice in a day. One switched-on integration is checked per tick,
the one whose last check is oldest, and only once it is a day old.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text

from core.auth import trail_events
from core.integrations.credential_check import NotChecked, check_credentials, has_check
from core.integrations.readiness import NOT_READY, READY, evaluate
from core.platform.process_health import ProcessHealth
from core.reporting import capped_recheck, day1, production
from core.storage import source_health
from core.storage.models import IntegrationConfig, SourceHealth
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

SWEEP_SECONDS = 3600
FIRST_SWEEP_SECONDS = 60
RECHECK_AFTER = timedelta(hours=24)
# Advisory lock key. Distinct from the dormancy sweep's.
LOCK = 4874
REACHED = "install.readiness.reached"
LOST = "install.readiness.lost"


async def run() -> None:
    processes = ProcessHealth()
    try:
        await asyncio.sleep(FIRST_SWEEP_SECONDS)
        while True:
            try:
                await tick(processes)
            except Exception:  # noqa: BLE001 - the next sweep tries again
                logger.exception("install readiness sweep failed")
            await asyncio.sleep(SWEEP_SECONDS)
    finally:
        await processes.aclose()


async def tick(processes: ProcessHealth) -> None:
    """One pass: the audit decision, a report when one is due, then at most one re-check."""
    state = await evaluate(processes)
    source_id, started, production_started = await asyncio.to_thread(_commit, state)
    if started:
        run_id, trigger = started
        try:
            gateway = await day1.read_gateway()
            record_id = await asyncio.to_thread(
                day1.finish, run_id, trigger, None, state, gateway
            )
        except (
            Exception
        ):  # noqa: BLE001 - the start stays; the next tick does not retry it
            logger.exception("day 1 report did not complete")
        else:
            await asyncio.to_thread(day1.notify_finished, trigger, None, record_id)
    if production_started:
        run_id, trigger = production_started
        try:
            record_id = await asyncio.to_thread(
                production.finish, run_id, trigger, None, state
            )
        except (
            Exception
        ):  # noqa: BLE001 - the start stays; the next tick does not retry it
            logger.exception("production start report did not complete")
        else:
            await asyncio.to_thread(
                production.notify_finished, trigger, None, record_id
            )
    if source_id:
        await _recheck(source_id)


def _commit(
    state: dict,
) -> tuple[Optional[str], Optional[tuple[str, str]], Optional[tuple[str, str]]]:
    started: Optional[tuple[str, str]] = None
    production_started: Optional[tuple[str, str]] = None
    try:
        with unit_of_work() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK})
            _audit(session, state["state"])
            source_id = _claim(session)
            started = day1.consider(session, state)
            production_started = production.consider(session)
            capped_recheck.consider(session)
        return source_id, started, production_started
    except Exception:
        if started is not None:
            day1.abandon(started[0])
        if production_started is not None:
            production.abandon(production_started[0])
        raise


def _audit(session, status: str) -> None:
    if status not in (READY, NOT_READY):
        return
    if not trail_events.ready(session):
        return
    was_ready = _latest(session) == REACHED
    ready = status == READY
    if ready and not was_ready:
        trail_events.record(session, REACHED, actor="system")
    elif was_ready and not ready:
        trail_events.record(session, LOST, actor="system")


def _latest(session) -> Optional[str]:
    row = session.execute(
        text("""
            SELECT payload->>'action' AS action
            FROM public.audit_trail
            WHERE payload->>'action' IN (:reached, :lost)
            ORDER BY seq DESC
            LIMIT 1
            """),
        {"reached": REACHED, "lost": LOST},
    ).first()
    return row.action if row else None


def _claim(session) -> Optional[str]:
    """The switched-on integration whose last check is oldest, if it is due."""
    now = utcnow()
    enabled = [
        integration_id
        for (integration_id,) in session.query(IntegrationConfig.integration_id)
        .filter(IntegrationConfig.enabled.is_(True))
        .all()
    ]
    due: list[tuple[datetime, str]] = []
    for integration_id in enabled:
        if not has_check(integration_id):
            continue
        row = session.get(SourceHealth, integration_id)
        checked = row.last_check_at if row is not None else None
        if checked is not None and now - checked < RECHECK_AFTER:
            continue
        due.append((checked or datetime.min, integration_id))
    if not due:
        return None
    due.sort()
    integration_id = due[0][1]
    source_health.stamp_recheck(session, integration_id, now)
    return integration_id


async def _recheck(source_id: str) -> None:
    try:
        outcome = await check_credentials(source_id)
    except Exception:  # noqa: BLE001 - the claim already moved the rotation on
        logger.exception("install readiness re-check failed to run")
        return
    if outcome is None or isinstance(outcome, NotChecked):
        return
    success = bool(outcome[0])
    error = None if success else outcome[1]
    kind = None
    if not success:
        kind = outcome[2] if len(outcome) > 2 and outcome[2] else "unknown"
    await asyncio.to_thread(
        source_health.record_recheck,
        source_id,
        success=success,
        error=error,
        kind=kind,
    )
