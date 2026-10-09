"""``GET /sla-policies/{id}/usage?since=`` narrows every count to one window.

The table reads "Met this month" off it, so total, active, breached and the
compliance rate must all move together. Without ``since`` the answer is the
all-time one, unchanged.

DB-backed on purpose: the window is a ``WHERE`` clause, and a fake session
that ignores ``filter()`` asserts nothing about it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.cases import sla_policies_router as router
from core.storage.connection import get_db_session
from core.storage.models import Case, CaseSLA, SLAPolicy
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

POLICY = "sla-test-usage-since"
CASES = ["case-usage-old", "case-usage-new-ok", "case-usage-new-late"]


def _wipe(db) -> None:
    db.query(CaseSLA).filter(CaseSLA.case_id.in_(CASES)).delete()
    db.query(Case).filter(Case.case_id.in_(CASES)).delete()
    db.query(SLAPolicy).filter(SLAPolicy.policy_id == POLICY).delete()
    db.commit()


@pytest.fixture
def session():
    db = get_db_session()
    try:
        _wipe(db)
        yield db
    finally:
        db.rollback()
        _wipe(db)
        db.close()


def _seed(db, since: datetime) -> None:
    """One breached case before ``since``; one met and one breached after it."""
    db.add(
        SLAPolicy(
            policy_id=POLICY,
            name=POLICY,
            priority_level="high",
            response_time_hours=1.0,
            resolution_time_hours=8.0,
        )
    )
    db.flush()
    rows = [
        (CASES[0], "closed", True, since - timedelta(days=3)),
        (CASES[1], "open", False, since + timedelta(days=1)),
        (CASES[2], "closed", True, since + timedelta(days=2)),
    ]
    for case_id, status, breached, created in rows:
        db.add(Case(case_id=case_id, title=case_id, status=status, priority="high", created_at=created))
        db.flush()
        db.add(
            CaseSLA(
                case_id=case_id,
                sla_policy_id=POLICY,
                response_due=created + timedelta(hours=1),
                resolution_due=created + timedelta(hours=8),
                breached=breached,
                created_at=created,
            )
        )
    db.flush()


def test_without_since_every_case_counts(session):
    _seed(session, utcnow())

    usage = router.get_policy_usage(POLICY, session=session)

    assert usage["total_cases"] == 3
    assert usage["active_cases"] == 1
    assert usage["breached_cases"] == 2
    assert usage["compliance_rate"] == 33.33


def test_since_narrows_total_active_breached_and_compliance(session):
    since = utcnow()
    _seed(session, since)

    usage = router.get_policy_usage(POLICY, session=session, since=since)

    assert usage["total_cases"] == 2
    assert usage["active_cases"] == 1
    assert usage["breached_cases"] == 1
    assert usage["compliance_rate"] == 50.0


def test_a_window_with_no_cases_counts_zero(session):
    since = utcnow()
    _seed(session, since)

    usage = router.get_policy_usage(
        POLICY, session=session, since=since + timedelta(days=30)
    )

    assert usage["total_cases"] == 0
    assert usage["compliance_rate"] == 0.0


def test_a_zoned_since_is_read_as_the_same_instant(session):
    since = utcnow()
    _seed(session, since)

    zoned = since.replace(tzinfo=timezone.utc)
    usage = router.get_policy_usage(POLICY, session=session, since=zoned)

    assert usage["total_cases"] == 2
