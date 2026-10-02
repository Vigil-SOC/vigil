"""Case metrics read MTTR/MTTD from case data, in a fixed number of queries.

``case_metrics`` is not populated in normal operation (#1435), so the MTTR,
MTTD and analyst-performance reads are derived from the closure timestamp and
the activity log. None of these tests writes a ``case_metrics`` row.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from core.api.v1.case_metrics_router import (
    get_all_analyst_performance,
    get_mttd,
    get_mttr,
)
from core.storage.connection import get_db_session
from core.storage.models import Case, CaseClosureInfo, CaseMetrics

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

T0 = datetime(2026, 6, 1, 9, 0, 0)
HOUR = timedelta(hours=1)


@pytest.fixture
def session():
    db = get_db_session()
    try:
        db.query(Case).delete()
        db.commit()
        yield db
    finally:
        db.rollback()
        db.close()


def _case(
    session,
    case_id: str,
    *,
    status: str,
    priority: str = "medium",
    assignee: str | None = None,
    created_at: datetime = T0,
    updated_at: datetime | None = None,
    closed_at: datetime | None = None,
    first_activity: datetime | None = None,
    stamp_suffix: str = "",
) -> None:
    activities = []
    if first_activity is not None:
        # Out of order, so "first" has to mean the minimum, not the head.
        activities = [
            {"timestamp": (first_activity + HOUR).isoformat() + stamp_suffix},
            {"timestamp": first_activity.isoformat() + stamp_suffix},
            {"timestamp": "not a timestamp"},
        ]
    session.add(
        Case(
            case_id=case_id,
            title=case_id,
            status=status,
            priority=priority,
            assignee=assignee,
            created_at=created_at,
            updated_at=updated_at or created_at,
            activities=activities,
        )
    )
    session.flush()
    if closed_at is not None:
        session.add(
            CaseClosureInfo(
                case_id=case_id,
                closure_category="unspecified",
                closed_by="tester",
                closed_by_kind="analyst",
                closed_at=closed_at,
            )
        )
        session.flush()


def _seed(session) -> None:
    # Closed via close_case: the closure row is authoritative, and a later
    # edit (updated_at) must not stretch the resolution time.
    _case(
        session,
        "c-high-1",
        status="closed",
        priority="high",
        assignee="alice",
        closed_at=T0 + 2 * HOUR,
        updated_at=T0 + 50 * HOUR,
        first_activity=T0 + timedelta(minutes=30),
        stamp_suffix="Z",
    )
    _case(
        session,
        "c-high-2",
        status="closed",
        priority="high",
        assignee="alice",
        created_at=T0 + timedelta(days=1),
        closed_at=T0 + timedelta(days=1) + 4 * HOUR,
        first_activity=T0 + timedelta(days=1) + 1.5 * HOUR,
    )
    # Resolved by a status edit: no closure row, updated_at is the close.
    _case(
        session,
        "c-low-1",
        status="resolved",
        priority="low",
        assignee="bob",
        updated_at=T0 + 9 * HOUR,
    )
    # Open cases count toward MTTD and assignment, never toward MTTR.
    _case(
        session,
        "o-high-1",
        status="open",
        priority="high",
        assignee="alice",
        updated_at=T0 + 100 * HOUR,
        first_activity=T0 + 3.5 * HOUR,
    )
    _case(session, "o-low-1", status="investigating", priority="low", assignee="")
    session.commit()


def _run(handler, session, **kwargs):
    kwargs.setdefault("start_date", None)
    kwargs.setdefault("end_date", None)
    return handler(session=session, **kwargs)


def test_mttr_comes_from_closure_time_not_case_metrics(session):
    _seed(session)
    assert session.query(CaseMetrics).count() == 0

    result = _run(get_mttr, session, priority=None)

    # (2 + 4 + 9) / 3 hours
    assert result["total_cases"] == 3
    assert result["average_mttr_hours"] == pytest.approx(5.0)
    assert result["average_mttr_seconds"] == pytest.approx(5.0 * 3600)
    assert result["mttr_by_priority"] == {
        "high": pytest.approx(3.0),
        "low": pytest.approx(9.0),
    }
    assert result["trend_data"] == [
        # c-high-1 (mttr 2h, mttd 0.5h) and c-low-1 (mttr 9h, no activity)
        {"date": "2026-06-01", "mttd": pytest.approx(0.5), "mttr": 5.5},
        {"date": "2026-06-02", "mttd": pytest.approx(1.5), "mttr": 4.0},
    ]


def test_mttr_filters_by_priority_and_window(session):
    _seed(session)

    high = _run(get_mttr, session, priority="high")
    assert high["total_cases"] == 2
    assert high["average_mttr_hours"] == pytest.approx(3.0)

    second_day = _run(
        get_mttr,
        session,
        priority=None,
        start_date=T0 + timedelta(hours=12),
    )
    assert second_day["total_cases"] == 1
    assert second_day["average_mttr_hours"] == pytest.approx(4.0)


def test_mttd_is_first_activity_over_every_case(session):
    _seed(session)

    result = _run(get_mttd, session, priority=None)

    # Measured: 0.5h, 1.5h (closed) and 3.5h (open); two cases have no activity.
    assert result["total_cases"] == 5
    assert result["average_mttd_hours"] == pytest.approx(5.5 / 3)
    assert result["mttd_by_priority"] == {"high": pytest.approx(5.5 / 3)}


@pytest.mark.parametrize(
    "bad_stamp",
    ["2026-02-30T10:00:00", "2026-06-01T10:00:00garbage", "2026-06-01T10:00:00+99"],
)
def test_malformed_activity_timestamp_is_skipped_not_fatal(session, bad_stamp):
    _case(
        session,
        "good",
        status="closed",
        closed_at=T0 + 2 * HOUR,
        first_activity=T0 + HOUR,
    )
    _case(session, "bad", status="closed", closed_at=T0 + 2 * HOUR)
    session.query(Case).filter(Case.case_id == "bad").update(
        {"activities": [{"timestamp": bad_stamp}]}
    )
    session.commit()

    mttd = _run(get_mttd, session, priority=None)
    mttr = _run(get_mttr, session, priority=None)

    assert mttd["total_cases"] == 2
    assert mttd["average_mttd_hours"] == pytest.approx(1.0)
    assert mttr["average_mttr_hours"] == pytest.approx(2.0)


def test_no_measured_case_reports_null_not_zero(session):
    _case(session, "o-only", status="open")
    session.commit()

    mttr = _run(get_mttr, session, priority=None)
    mttd = _run(get_mttd, session, priority=None)

    assert mttr["total_cases"] == 0
    assert mttr["average_mttr_hours"] is None
    assert mttr["average_mttr_seconds"] is None
    assert mttr["mttr_by_priority"] == {}
    assert mttd["total_cases"] == 1
    assert mttd["average_mttd_hours"] is None


def test_analyst_performance_breakdown(session):
    _seed(session)

    result = _run(get_all_analyst_performance, session)

    assert result["analyst_performance"] == [
        {
            "analyst_id": "alice",
            "analyst_name": "alice",
            "cases_assigned": 3,
            "cases_resolved": 2,
            "avg_resolution_time": pytest.approx(3.0),
        },
        {
            "analyst_id": "bob",
            "analyst_name": "bob",
            "cases_assigned": 1,
            "cases_resolved": 1,
            "avg_resolution_time": pytest.approx(9.0),
        },
        {
            "analyst_id": "unassigned",
            "analyst_name": "unassigned",
            "cases_assigned": 1,
            "cases_resolved": 0,
            "avg_resolution_time": 0,
        },
    ]


def _count_queries(session, fn) -> int:
    statements = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(conn, "before_cursor_execute", before)
    return len(statements)


@pytest.mark.parametrize(
    "handler,kwargs",
    [
        (get_mttr, {"priority": None}),
        (get_mttd, {"priority": None}),
        (get_all_analyst_performance, {}),
    ],
    ids=["mttr", "mttd", "analyst-performance"],
)
def test_query_count_does_not_grow_with_cases(session, handler, kwargs):
    def seed(prefix: str, n: int) -> None:
        for i in range(n):
            _case(
                session,
                f"{prefix}-{i}",
                status="closed" if i % 2 else "open",
                priority=("high", "low")[i % 2],
                assignee=f"analyst-{i % 3}",
                created_at=T0 + timedelta(days=i % 4),
                closed_at=T0 + timedelta(days=i % 4) + HOUR if i % 2 else None,
                first_activity=T0 + timedelta(days=i % 4, minutes=10),
            )
        session.commit()

    seed("small", 4)
    small = _count_queries(session, lambda: _run(handler, session, **kwargs))
    seed("large", 60)
    large = _count_queries(session, lambda: _run(handler, session, **kwargs))

    assert small == large == 1
