"""Two people editing a case: per-field compare-and-write on PATCH.

Runs the real app against a real Postgres, because the guarantee is the row lock
and nothing else can show it. Point ``VIGIL_TEST_POSTGRES_URL`` at a throwaway
database that ``python -m core.upgrade`` has been applied to, for example
``postgresql+psycopg2://vigil:pw@127.0.0.1:5432/vigil``. Rows are left behind.
"""

import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from core.auth.current_user import get_current_user
from core.storage.models import Case, CaseClosureInfo
from core.storage.models.case_entities import CaseSLA, SLAPolicy
from core.storage.service import DatabaseService

URL = os.environ.get("VIGIL_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="VIGIL_TEST_POSTGRES_URL is not set")

MOUNTS = ["/api/cases", "/api/v1/cases"]
ANALYST = MagicMock(user_id="u-analyst", username="analyst-a")


@pytest.fixture(scope="module")
def sessions():
    engine = create_engine(URL)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def app(sessions, monkeypatch):
    from core.api.v1 import cases_router
    from services.api.main import app as vigil

    monkeypatch.setattr("core.storage.unit_of_work.get_db_session", lambda: sessions())
    monkeypatch.setattr(
        "core.auth.permissions.missing_permission", lambda *a, **k: None
    )
    service = DatabaseService()
    monkeypatch.setattr(service.db_manager, "_session_factory", sessions)
    monkeypatch.setattr(cases_router.data_service, "_db_connected", True)
    monkeypatch.setattr(cases_router.data_service, "_db_service", service)
    vigil.dependency_overrides[get_current_user] = lambda: ANALYST
    yield vigil
    vigil.dependency_overrides.pop(get_current_user)


@pytest.fixture
def upstream(monkeypatch):
    """The SIEM a status change is pushed to: outside the database, so outside its rollback."""
    from core.api.v1 import cases_router

    sync = MagicMock()
    monkeypatch.setattr(cases_router, "_sync_upstream_status", sync)
    return sync


@pytest.fixture
def client(app, upstream):
    return TestClient(app)


@pytest.fixture
def case_id(sessions):
    case = f"case-stale-{uuid.uuid4().hex[:12]}"
    policy = f"sla-stale-{uuid.uuid4().hex[:12]}"
    now = datetime(2030, 1, 1)
    with sessions.begin() as session:
        session.add(
            Case(
                case_id=case,
                title="Beaconing from lab host",
                description="Synthetic case",
                status="open",
                priority="medium",
                assignee=None,
                notes=[],
            )
        )
        session.add(
            SLAPolicy(
                policy_id=policy,
                name=f"Synthetic {policy}",
                priority_level="medium",
                response_time_hours=1.0,
                resolution_time_hours=2.0,
            )
        )
        session.flush()
        session.add(
            CaseSLA(
                case_id=case,
                sla_policy_id=policy,
                response_due=now + timedelta(hours=1),
                resolution_due=now + timedelta(hours=2),
            )
        )
    return case


def _record(sessions, case_id):
    """Everything an edit could have touched, read back through its own connection."""
    with sessions() as session:
        case = session.get(Case, case_id)
        sla = session.scalars(select(CaseSLA).where(CaseSLA.case_id == case_id)).one()
        trail = session.scalar(
            text("SELECT count(*) FROM audit_trail WHERE payload::text LIKE :needle"),
            {"needle": f"%{case_id}%"},
        )
        legacy = session.scalar(
            text("SELECT count(*) FROM case_audit_logs WHERE entity_id = :id"),
            {"id": case_id},
        )
        return {
            "title": case.title,
            "description": case.description,
            "status": case.status,
            "priority": case.priority,
            "assignee": case.assignee,
            "notes": case.notes,
            "updated_at": case.updated_at,
            "trail_entries": trail + legacy,
            "sla_resolved_at": sla.resolution_completed_at,
            "closure": session.get(CaseClosureInfo, case_id) is not None,
        }


@pytest.mark.parametrize("mount", MOUNTS)
def test_a_conflict_answers_409_and_writes_nothing(
    client, sessions, case_id, upstream, mount
):
    before = _record(sessions, case_id)

    res = client.patch(
        f"{mount}/{case_id}",
        json={
            "status": "closed",
            "title": "Mine",
            "notes": "Closing this",
            "expected": {"status": "investigating", "title": "Beaconing from lab host"},
        },
    )

    assert res.status_code == 409
    assert res.json()["conflicts"] == [
        {"field": "status", "expected": "investigating", "current": "open"}
    ]
    assert _record(sessions, case_id) == before
    upstream.assert_not_called()


def test_the_same_edit_with_the_right_values_has_every_effect(
    client, sessions, case_id, upstream
):
    """The test above could pass by looking in the wrong place. This one sees them all."""
    before = _record(sessions, case_id)

    res = client.patch(
        f"/api/cases/{case_id}",
        json={
            "status": "closed",
            "title": "Mine",
            "notes": "Closing this",
            "expected": {"status": "open", "title": "Beaconing from lab host"},
        },
    )

    assert res.status_code == 200, res.text
    after = _record(sessions, case_id)
    assert after["status"] == "closed"
    assert after["title"] == "Mine"
    assert [note["content"] for note in after["notes"]] == ["Closing this"]
    assert after["updated_at"] != before["updated_at"]
    assert after["trail_entries"] > before["trail_entries"]
    assert after["sla_resolved_at"] is not None
    assert after["closure"] is True
    upstream.assert_called_once_with(case_id, "closed")


def test_editing_different_fields_both_succeed(client, sessions, case_id):
    first = client.patch(
        f"/api/cases/{case_id}",
        json={"title": "Theirs", "expected": {"title": "Beaconing from lab host"}},
    )
    second = client.patch(
        f"/api/cases/{case_id}",
        json={"priority": "high", "expected": {"priority": "medium"}},
    )

    assert (first.status_code, second.status_code) == (200, 200)
    after = _record(sessions, case_id)
    assert (after["title"], after["priority"]) == ("Theirs", "high")


def test_a_run_touching_the_case_is_not_a_conflict(client, sessions, case_id):
    """The whole-row version this replaces would refuse here."""
    from core.api.v1 import cases_router

    with sessions.begin() as session:
        cases_router.data_service.update_case(
            case_id, session=session, activities=[{"from": "a run"}]
        )

    res = client.patch(
        f"/api/cases/{case_id}",
        json={"priority": "high", "expected": {"priority": "medium"}},
    )

    assert res.status_code == 200, res.text


def test_blank_and_null_are_the_same_expected_value(client, sessions, case_id):
    res = client.patch(
        f"/api/cases/{case_id}",
        json={"title": "Mine", "expected": {"assignee": "", "title": None}},
    )

    assert res.status_code == 409
    assert [c["field"] for c in res.json()["conflicts"]] == ["title"]

    res = client.patch(
        f"/api/cases/{case_id}",
        json={"description": "Mine", "expected": {"assignee": ""}},
    )

    assert res.status_code == 200, res.text


@pytest.mark.parametrize("mount", MOUNTS)
def test_no_expected_writes_as_it_always_has(client, sessions, case_id, mount):
    client.patch(f"{mount}/{case_id}", json={"priority": "low"})

    res = client.patch(f"{mount}/{case_id}", json={"priority": "high"})

    assert res.status_code == 200, res.text
    assert res.json() == {"success": True}
    assert _record(sessions, case_id)["priority"] == "high"


def test_an_unknown_expected_field_is_refused(client, case_id):
    res = client.patch(
        f"/api/cases/{case_id}",
        json={"priority": "high", "expected": {"tags": "anything"}},
    )

    assert res.status_code == 422


def test_a_missing_case_is_a_404(client):
    res = client.patch(
        "/api/cases/case-not-there",
        json={"priority": "high", "expected": {"priority": "medium"}},
    )

    assert res.status_code == 404


def _blocked_on_a_lock(sessions) -> int:
    with sessions() as session:
        return session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND query ILIKE '%cases%'"
            )
        )


def test_two_connections_racing_exactly_one_wins(client, sessions, case_id):
    """Both requests are parked behind a held row lock, so both have read by release."""
    url = f"/api/cases/{case_id}"
    answers: dict[str, object] = {}

    def edit(priority: str) -> None:
        answers[priority] = client.patch(
            url, json={"priority": priority, "expected": {"priority": "medium"}}
        )

    with sessions() as gate:
        gate.execute(
            select(Case).where(Case.case_id == case_id).with_for_update()
        ).first()
        racers = [threading.Thread(target=edit, args=(p,)) for p in ("high", "low")]
        for racer in racers:
            racer.start()
        deadline = time.monotonic() + 15
        while _blocked_on_a_lock(sessions) < 2:
            assert time.monotonic() < deadline, "the racers never reached the lock"
            time.sleep(0.05)
        gate.rollback()

    for racer in racers:
        racer.join(timeout=15)

    codes = sorted(res.status_code for res in answers.values())
    assert codes == [200, 409], {k: v.text for k, v in answers.items()}
    winner = next(p for p, res in answers.items() if res.status_code == 200)
    loser = next(res for res in answers.values() if res.status_code == 409)
    assert _record(sessions, case_id)["priority"] == winner
    assert loser.json()["conflicts"] == [
        {"field": "priority", "expected": "medium", "current": winner}
    ]
