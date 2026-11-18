"""Catch the shared IOC index up on its own (#4734, epic #4580).

A write that failed stays unindexed, and a Case's findings can change without a
write (retention, a missed hook). The daemon calls :func:`catch_up_once` on
each tick: it reconciles a batch of Cases, one short transaction each, so replicas
split the work. Deterministic repair of a derived copy, not a retry ladder.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Tuple

from sqlalchemy import select, update

from core.storage.models import Case
from core.storage.shared_ioc_repository import SharedIOCRepository
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

# How long an indicator may wait, measured from indicators_stale_since, before an
# admin is told. Read by the bell conditions only; the loop never looks at it.
LAG_LIMIT = timedelta(minutes=15)

BATCH = 300
# Stale Cases that fail every tick must not fill the batch, so each group has a
# share: stale at most STALE_QUOTA, and the drift sweep (the Cases reconciled
# longest ago, which repairs retention-deleted findings and missed writes) a few.
STALE_QUOTA = 100
DRIFT_QUOTA = 25


@dataclass
class CatchUp:
    reconciled: int = 0
    failed: int = 0
    skipped: int = 0  # claimed by another replica, or already done since the pick


def _pick(session, *where, order, limit: int) -> List[str]:
    return list(
        session.scalars(
            select(Case.case_id).where(*where).order_by(*order).limit(limit)
        )
    )


def _batch(size: int) -> List[Tuple[str, str]]:
    """``(case_id, group)``: stale oldest first, then the oldest synced."""
    with unit_of_work() as session:
        stale = _pick(
            session,
            Case.indicators_stale_since.is_not(None),
            order=(Case.indicators_stale_since, Case.case_id),
            limit=min(STALE_QUOTA, size),
        )
        drift = _pick(
            session,
            # Always true now, but it lets idx_case_indicators_synced (partial) serve.
            Case.indicators_synced_at.is_not(None),
            order=(Case.indicators_synced_at, Case.case_id),
            limit=DRIFT_QUOTA,
        )
    # A Case can be in both groups (stale and the oldest synced); once is enough.
    seen = set()
    out = []
    for group, ids in (("stale", stale), ("drift", drift)):
        for case_id in ids:
            if case_id not in seen:
                seen.add(case_id)
                out.append((case_id, group))
    return out


def _reconcile(case_id: str, group: str, started) -> bool:
    """Reconcile one Case in its own transaction. False if someone else has it or
    it no longer needs it. Raises if the reconcile fails (and rolls back)."""
    with unit_of_work() as session:
        # Claim: a row another replica holds is skipped, and the group's own
        # condition is read again so a Case it just finished is not done twice.
        need = {
            "stale": Case.indicators_stale_since.is_not(None),
            "drift": Case.indicators_synced_at < started,
        }[group]
        claimed = session.scalar(
            select(Case.case_id)
            .where(Case.case_id == case_id, need)
            .with_for_update(skip_locked=True)
        )
        if claimed is None:
            return False
        return SharedIOCRepository(session).reconcile_case(case_id)


def _mark_stale(case_id: str) -> None:
    """After a failed reconcile: stale if not already, never cleared here."""
    try:
        with unit_of_work() as session:
            session.execute(
                update(Case).where(
                    Case.case_id == case_id, Case.indicators_stale_since.is_(None)
                )
                # Not an edit of the Case: leave updated_at alone.
                .values(indicators_stale_since=utcnow(), updated_at=Case.updated_at)
            )
    except Exception as e:
        logger.error("Failed to mark %s stale: %s", case_id, e)


def catch_up_once(size: int = BATCH) -> CatchUp:
    """Reconcile one batch. A Case that fails is logged and left stale; the rest go on."""
    started = utcnow()
    result = CatchUp()
    for case_id, group in _batch(size):
        try:
            if _reconcile(case_id, group, started):
                result.reconciled += 1
            else:
                result.skipped += 1
        except Exception as e:
            result.failed += 1
            logger.error("Failed to reconcile %s: %s", case_id, e, exc_info=True)
            _mark_stale(case_id)
    return result
