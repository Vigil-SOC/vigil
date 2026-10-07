"""The case queue is one SQL page: SLA order, closed, at risk, empty."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from core.cases.combined_state import queue_item
from core.storage.case_repository import CaseRepository
from core.storage.connection import get_db_session
from core.storage.models import (
    Case,
    CaseClosureInfo,
    CaseComment,
    CaseSLA,
    Finding,
    Investigation,
    SLAPolicy,
)

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

NOW = datetime(2026, 6, 15, 12, 0, 0)


@pytest.fixture
def session():
    db = get_db_session()
    try:
        db.query(Investigation).delete()
        db.query(Case).delete()
        db.query(Finding).delete()
        db.query(SLAPolicy).delete()
        db.commit()
        yield db
    finally:
        db.rollback()
        db.close()


def _policy(session) -> None:
    session.add(
        SLAPolicy(
            policy_id="p-queue",
            name="queue",
            priority_level="medium",
            response_time_hours=1,
            resolution_time_hours=24,
        )
    )
    session.flush()


def _case(
    session,
    case_id: str,
    *,
    status: str = "open",
    priority: str = "medium",
    assignee: str | None = None,
    updated_at: datetime | None = None,
    created_at: datetime | None = None,
) -> Case:
    updated_at = updated_at or NOW
    case = Case(
        case_id=case_id,
        title=case_id,
        status=status,
        priority=priority,
        assignee=assignee,
        created_at=created_at or updated_at,
        updated_at=updated_at,
    )
    session.add(case)
    session.flush()
    return case


def _sla(
    session,
    case_id: str,
    *,
    created_at: datetime,
    resolution_due: datetime,
    response_due: datetime | None = None,
    response_completed: bool = True,
    resolution_completed: bool = False,
    paused: bool = False,
) -> None:
    session.add(
        CaseSLA(
            case_id=case_id,
            sla_policy_id="p-queue",
            response_due=response_due or (NOW + timedelta(days=30)),
            resolution_due=resolution_due,
            response_completed_at=NOW if response_completed else None,
            resolution_completed_at=NOW if resolution_completed else None,
            is_paused=paused,
            created_at=created_at,
        )
    )
    session.flush()


def _inv(
    session,
    inv_id: str,
    case_id: str,
    *,
    status: str = "completed",
    workflow_id: str = "wf",
    created_at: datetime,
    last_activity_at: datetime | None = None,
    cost: float = 0.0,
    max_cost: float = 5.0,
    iterations: int = 1,
) -> None:
    session.add(
        Investigation(
            investigation_id=inv_id,
            case_id=case_id,
            workflow_id=workflow_id,
            trigger_type="finding",
            trigger_ids=[],
            status=status,
            workdir="",
            created_at=created_at,
            last_activity_at=last_activity_at,
            cost_usd=cost,
            max_cost_usd=max_cost,
            iteration_count=iterations,
        )
    )
    session.flush()


def _ids(rows) -> list[str]:
    return [row.case_id for row in rows]


def _sql(session, fn):
    captured = []

    def before(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    conn = session.connection()
    event.listen(conn, "before_cursor_execute", before)
    try:
        fn()
    finally:
        event.remove(conn, "before_cursor_execute", before)
    return captured


def _order_by(statements) -> str:
    pages = [statement for statement in statements if "ORDER BY" in statement]
    assert pages, statements
    return pages[-1].rsplit("ORDER BY", 1)[1]


def _shown(rows, now=NOW):
    return [queue_item(row, now) for row in rows]


def test_default_queue_is_soonest_resolution_first_with_no_sla_last(session):
    _policy(session)
    # Overdue, then one hour, then two cases sharing a due (newer activity
    # first), then a case with no SLA even though it was touched last.
    _case(session, "overdue", updated_at=NOW - timedelta(hours=8))
    _sla(
        session,
        "overdue",
        created_at=NOW - timedelta(hours=10),
        resolution_due=NOW - timedelta(hours=1),
    )
    _case(session, "soon", updated_at=NOW - timedelta(hours=8))
    _sla(
        session,
        "soon",
        created_at=NOW - timedelta(hours=10),
        resolution_due=NOW + timedelta(hours=1),
    )
    _case(session, "tie-new", updated_at=NOW - timedelta(hours=4))
    _sla(
        session,
        "tie-new",
        created_at=NOW - timedelta(hours=10),
        resolution_due=NOW + timedelta(hours=2),
    )
    _inv(
        session,
        "inv-tie-new",
        "tie-new",
        created_at=NOW - timedelta(hours=3),
        last_activity_at=NOW - timedelta(minutes=30),
    )
    _case(session, "tie-old", updated_at=NOW - timedelta(hours=5))
    _sla(
        session,
        "tie-old",
        created_at=NOW - timedelta(hours=10),
        resolution_due=NOW + timedelta(hours=2),
    )
    _case(session, "no-sla", updated_at=NOW)
    # Past due, but the clock is not running, so they sort after live clocks.
    _case(session, "paused", updated_at=NOW - timedelta(hours=1))
    _sla(
        session,
        "paused",
        created_at=NOW - timedelta(hours=10),
        resolution_due=NOW - timedelta(hours=5),
        paused=True,
    )
    _case(session, "met", updated_at=NOW - timedelta(hours=2))
    _sla(
        session,
        "met",
        created_at=NOW - timedelta(hours=10),
        resolution_due=NOW - timedelta(hours=5),
        resolution_completed=True,
    )
    # Sooner than every open case, and still excluded.
    _case(session, "closed-soon", status="closed", updated_at=NOW)
    _sla(
        session,
        "closed-soon",
        created_at=NOW - timedelta(hours=10),
        resolution_due=NOW - timedelta(hours=3),
    )

    rows, total = CaseRepository(session).queue(now=NOW)
    shown = _shown(rows)

    assert total == 7
    assert _ids(rows) == [
        "overdue",
        "soon",
        "tie-new",
        "tie-old",
        "paused",
        "met",
        "no-sla",
    ]
    assert shown[0].sla_seconds_left < 0
    assert shown[-1].health_status is None
    assert shown[-1].sla_seconds_left is None
    by_id = {row.case_id: row for row in shown}
    assert by_id["paused"].sla_seconds_left is None
    assert by_id["met"].sla_seconds_left is None

    by_id_hit, id_total = CaseRepository(session).queue(query_text="tie-old", now=NOW)
    assert id_total == 1
    assert _ids(by_id_hit) == ["tie-old"]


def test_closed_filter_returns_only_closed_cases(session):
    _policy(session)
    _case(session, "open-one", priority="high", assignee="ada")
    _case(session, "closed-one", status="closed", priority="low", assignee="ada")
    _case(session, "closed-two", status="closed", priority="high")

    repo = CaseRepository(session)
    closed, total = repo.queue(closed=True, now=NOW)
    assert total == 2
    assert _ids(closed) == ["closed-one", "closed-two"]

    narrowed, narrowed_total = repo.queue(
        closed=True, priority="low", assignee="ada", now=NOW
    )
    assert narrowed_total == 1
    assert _ids(narrowed) == ["closed-one"]


def test_sla_at_risk_is_warning_critical_or_breached(session):
    _policy(session)

    def clock(case_id: str, *, elapsed_hours: int, total_hours: int = 100, **sla):
        _case(session, case_id)
        created = NOW - timedelta(hours=elapsed_hours)
        _sla(
            session,
            case_id,
            created_at=created,
            resolution_due=created + timedelta(hours=total_hours),
            **sla,
        )

    clock("healthy", elapsed_hours=10)
    clock("boundary", elapsed_hours=75)  # exactly 75%
    clock("warning", elapsed_hours=80)
    clock("critical", elapsed_hours=95)
    clock("under", elapsed_hours=74)
    clock(
        "breached",
        elapsed_hours=5,
        total_hours=4,  # due an hour ago
    )
    clock("paused", elapsed_hours=95, paused=True)
    # Response clock at 80%, resolution still healthy.
    _case(session, "response")
    created = NOW - timedelta(hours=80)
    _sla(
        session,
        "response",
        created_at=created,
        response_due=created + timedelta(hours=100),
        resolution_due=NOW + timedelta(days=10),
        response_completed=False,
    )
    _case(session, "no-sla")
    _case(session, "closed-risk", status="closed")
    _sla(
        session,
        "closed-risk",
        created_at=NOW - timedelta(hours=95),
        resolution_due=NOW - timedelta(hours=1),
    )

    rows, total = CaseRepository(session).queue(sla_at_risk=True, now=NOW)
    shown = _shown(rows)

    assert total == 5
    assert set(_ids(rows)) == {
        "boundary",
        "warning",
        "critical",
        "breached",
        "response",
    }
    by_id = {row.case_id: row for row in shown}
    assert by_id["boundary"].health_status == "warning"
    assert by_id["warning"].health_status == "warning"
    assert by_id["critical"].health_status == "critical"
    assert by_id["breached"].health_status == "breached"


def test_empty_page(session):
    _policy(session)
    _case(session, "only")
    repo = CaseRepository(session)

    rows, total = repo.queue(offset=50, limit=100, now=NOW)
    assert rows == []
    assert total == 1

    none, none_total = repo.queue(workflow="missing", now=NOW)
    assert none == []
    assert none_total == 0


def test_row_reads_the_latest_investigation_and_the_strip(session):
    _policy(session)
    case = _case(
        session,
        "live",
        status="investigating",
        priority="high",
        updated_at=NOW - timedelta(hours=2),
    )
    _sla(
        session,
        "live",
        created_at=NOW - timedelta(hours=10),
        resolution_due=NOW + timedelta(hours=10),
    )
    _inv(
        session,
        "inv-old",
        "live",
        workflow_id="old-wf",
        status="completed",
        created_at=NOW - timedelta(days=2),
        last_activity_at=NOW - timedelta(days=1),
        cost=1.0,
        iterations=2,
    )
    _inv(
        session,
        "inv-new",
        "live",
        workflow_id="new-wf",
        status="executing",
        created_at=NOW - timedelta(hours=1),
        last_activity_at=NOW - timedelta(minutes=5),
        cost=4.0,
        max_cost=5.0,
        iterations=7,
    )
    session.add(Finding(finding_id="f-1", data_source="splunk", status="new"))
    session.flush()
    case.findings = session.query(Finding).all()
    session.add_all(
        [
            CaseComment(case_id="live", author="ada", content="one"),
            CaseComment(case_id="live", author="ada", content="two"),
        ]
    )
    _case(session, "plain", status="open")
    _case(session, "done-agent", status="closed")
    _case(session, "done-analyst", status="closed")
    _case(session, "done-yesterday", status="closed")
    session.add_all(
        [
            CaseClosureInfo(
                case_id="done-agent",
                closure_category="resolved",
                closed_by="soc-automation",
                closed_by_kind="agent",
                closed_at=NOW,
            ),
            CaseClosureInfo(
                case_id="done-analyst",
                closure_category="resolved",
                closed_by="ada",
                closed_by_kind="analyst",
                closed_at=NOW - timedelta(hours=1),
            ),
            CaseClosureInfo(
                case_id="done-yesterday",
                closure_category="resolved",
                closed_by="soc-automation",
                closed_by_kind="agent",
                closed_at=NOW - timedelta(days=2),
            ),
        ]
    )
    session.flush()

    repo = CaseRepository(session)
    rows, _ = repo.queue(state="executing", now=NOW)
    assert len(rows) == 1
    row = queue_item(rows[0], NOW)
    assert row.combined_state == "executing"
    assert row.workflow_id == "new-wf"
    assert row.iteration_count == 7
    assert row.cost_usd == 4.0
    assert row.max_cost_usd == 5.0
    assert row.budget_health == "warning"
    assert row.comment_count == 2
    assert row.findings_count == 1
    assert row.last_activity == NOW - timedelta(minutes=5)

    splunk, splunk_total = repo.queue(data_source="splunk", now=NOW)
    assert splunk_total == 1
    assert _ids(splunk) == ["live"]
    assert repo.queue(data_source="elastic", now=NOW)[1] == 0

    strip = repo.strip(now=NOW)
    assert strip.by_state == {"executing": 1, "open": 1}
    assert strip.sla_at_risk == 0
    assert strip.closed_today == 2
    assert strip.agent_closure_share == 0.5


def test_needs_you_ids_sort_first_and_an_empty_set_keeps_sla_order(session):
    _policy(session)
    _case(session, "sooner", updated_at=NOW - timedelta(hours=3))
    _sla(
        session,
        "sooner",
        created_at=NOW - timedelta(hours=4),
        resolution_due=NOW + timedelta(minutes=5),
    )
    _case(session, "also-waiting", updated_at=NOW - timedelta(hours=1))
    _sla(
        session,
        "also-waiting",
        created_at=NOW - timedelta(hours=4),
        resolution_due=NOW + timedelta(hours=1),
    )
    _case(session, "waiting", updated_at=NOW - timedelta(hours=3))
    _sla(
        session,
        "waiting",
        created_at=NOW - timedelta(hours=4),
        resolution_due=NOW + timedelta(hours=10),
    )
    _case(session, "no-sla", updated_at=NOW)

    repo = CaseRepository(session)
    sla_order = ["sooner", "also-waiting", "waiting", "no-sla"]
    plain, plain_total = repo.queue(now=NOW)
    held = {}

    def _empty():
        held["empty"] = repo.queue(needs_you_ids=set(), now=NOW)

    empty_order = _order_by(_sql(session, _empty))
    empty, empty_total = held["empty"]
    assert plain_total == empty_total == 4
    assert _ids(plain) == _ids(empty) == sla_order
    assert "IN" not in empty_order

    def _first():
        held["first"] = repo.queue(
            needs_you_ids={"waiting", "also-waiting", "missing"}, now=NOW
        )

    first_order = _order_by(_sql(session, _first))
    rows, total = held["first"]
    assert total == 4
    assert _ids(rows) == ["also-waiting", "waiting", "sooner", "no-sla"]
    assert "IN" in first_order

    page, page_total = repo.queue(needs_you_ids={"waiting"}, limit=1, offset=1, now=NOW)
    assert page_total == 4
    assert _ids(page) == ["sooner"]
