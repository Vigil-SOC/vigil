"""A Case with a live Investigation cannot be deleted one at a time (#1001).

``DELETE /api/cases/{id}`` returns 409 while a finding-run is live on that
Case. Bulk reset kills those runs first. Hunts have no Case, so they never
block a single delete and are not killed by the bulk path.

DB-backed on purpose: hunt exclusion and case scoping live entirely in the
``WHERE`` clauses, and a fake session that ignores ``filter()`` asserts
nothing about them.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from core.cases import case_records_service
from core.storage.connection import get_db_session
from core.storage.models import Case, Investigation
from core.time import utcnow


class _User:
    def __init__(self, user_id: str):
        self.user_id = user_id


_ADMIN = _User("admin-1")


class _Audit:
    def __init__(self):
        self.user_ids: list[str] = []
        self.calls: list[dict] = []

    def record_audit(self, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def audit(monkeypatch):
    recorder = _Audit()

    def factory(user_id="system"):
        recorder.user_ids.append(user_id)
        return recorder

    monkeypatch.setattr("services.api.routers.cases.get_config_service", factory)
    return recorder

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture
def session():
    db = get_db_session()
    try:
        db.query(Investigation).delete()
        db.query(Case).delete()
        db.commit()
        yield db
    finally:
        db.rollback()
        db.close()


def _case(session, case_id: str) -> Case:
    case = Case(
        case_id=case_id,
        title=f"case {case_id}",
        status="open",
        priority="medium",
        created_at=utcnow(),
    )
    session.add(case)
    session.flush()
    return case


def _investigation(session, inv_id, case_id, status, workdir="") -> Investigation:
    inv = Investigation(
        investigation_id=inv_id,
        case_id=case_id,
        workflow_id="incident-response",
        trigger_type="finding",
        trigger_ids=[],
        status=status,
        workdir=workdir,
    )
    session.add(inv)
    session.flush()
    return inv


@pytest.mark.asyncio
async def test_delete_case_with_live_investigation_is_409(session, audit):
    from services.api.routers import cases

    _case(session, "case-1")
    _investigation(session, "inv-1", "case-1", "executing")

    with pytest.raises(HTTPException) as exc:
        await cases.delete_case("case-1", session, _ADMIN)

    assert exc.value.status_code == 409
    assert exc.value.detail == (
        "case has 1 live investigation (inv-1); kill or finish them first"
    )
    assert session.get(Case, "case-1") is not None
    assert session.get(Investigation, "inv-1").status == "executing"
    assert audit.calls == []


@pytest.mark.asyncio
async def test_the_409_names_every_blocking_run(session, audit):
    from services.api.routers import cases

    _case(session, "case-1")
    _investigation(session, "inv-1", "case-1", "executing")
    _investigation(session, "inv-2", "case-1", "waiting_approval")

    with pytest.raises(HTTPException) as exc:
        await cases.delete_case("case-1", session, _ADMIN)

    assert audit.calls == []

    assert "2 live investigations" in exc.value.detail
    assert "inv-1" in exc.value.detail and "inv-2" in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.usefixtures("audit")
async def test_a_finished_investigation_does_not_block_the_delete(session):
    from services.api.routers import cases

    _case(session, "case-1")
    _investigation(session, "inv-done", "case-1", "completed")

    assert await cases.delete_case("case-1", session, _ADMIN) == {"success": True}
    assert session.get(Case, "case-1") is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("audit")
async def test_a_hunt_does_not_block_the_delete(session):
    """A hunt has no case_id, so the case-scoped filter never sees it."""
    from services.api.routers import cases

    _case(session, "case-1")
    _investigation(session, "inv-hunt", None, "executing")

    assert await cases.delete_case("case-1", session, _ADMIN) == {"success": True}
    assert session.get(Case, "case-1") is None
    assert session.get(Investigation, "inv-hunt").status == "executing"


@pytest.mark.asyncio
@pytest.mark.usefixtures("audit")
async def test_a_live_run_on_another_case_does_not_block_the_delete(session):
    from services.api.routers import cases

    _case(session, "case-1")
    _case(session, "case-2")
    _investigation(session, "inv-other", "case-2", "executing")

    assert await cases.delete_case("case-1", session, _ADMIN) == {"success": True}


@pytest.mark.asyncio
async def test_deleting_an_unknown_case_is_404(session, audit):
    from services.api.routers import cases

    with pytest.raises(HTTPException) as exc:
        await cases.delete_case("nope", session, _ADMIN)

    assert exc.value.status_code == 404
    assert audit.calls == []


@pytest.mark.asyncio
async def test_successful_delete_records_who_and_which_case(session, audit):
    from services.api.routers import cases

    _case(session, "case-1")

    assert await cases.delete_case("case-1", session, _ADMIN) == {"success": True}
    assert audit.user_ids == ["admin-1"]
    assert audit.calls == [
        {
            "config_type": "cases",
            "config_key": "case-1",
            "action": "delete",
            "old_value": {"case_id": "case-1", "title": "case case-1"},
            "new_value": None,
            "change_reason": "Case deleted via API",
        }
    ]


@pytest.mark.asyncio
async def test_demo_delete_writes_no_audit(audit, monkeypatch):
    from services.api.routers import cases

    monkeypatch.setattr(cases.data_service, "is_demo_mode", lambda: True)
    monkeypatch.setattr(cases.data_service, "delete_case", lambda case_id: True)

    assert await cases.delete_case("case-1", None, _ADMIN) == {"success": True}
    assert audit.calls == []


@pytest.mark.asyncio
async def test_purge_records_the_caller_and_the_counts(session, audit, monkeypatch):
    from fastapi import BackgroundTasks

    from core.auth.auth_service import AuthService
    from services.api.routers import cases

    monkeypatch.setattr(
        AuthService, "check_permission", staticmethod(lambda *a, **k: True)
    )
    _case(session, "case-1")
    _case(session, "case-2")
    _investigation(session, "inv-live", "case-1", "executing")

    result = await cases.clear_all_cases(
        session=session,
        background_tasks=BackgroundTasks(),
        current_user=_ADMIN,
    )

    assert result["deleted"] == 2
    assert result["killed_investigations"] == 1
    assert audit.user_ids == ["admin-1"]
    assert audit.calls == [
        {
            "config_type": "cases",
            "config_key": "cases",
            "action": "delete_all",
            "old_value": {"count": 2, "killed_investigations": 1},
            "new_value": None,
            "change_reason": "All cases deleted via API",
        }
    ]


def test_bulk_reset_kills_live_case_runs_and_leaves_hunts(session):
    _case(session, "case-1")
    live = _investigation(session, "inv-live", "case-1", "executing")
    hunt = _investigation(session, "inv-hunt", None, "executing")
    done = _investigation(session, "inv-done", "case-1", "completed")

    result = case_records_service.purge_all_cases(session)

    assert result.cases == 1
    assert result.killed_investigation_ids == ["inv-live"]
    assert live.status == "failed"
    assert live.master_review_notes == case_records_service.RESET_KILL_REASON
    assert hunt.status == "executing"
    assert done.status == "completed"


def test_bulk_reset_leaves_case_id_for_the_fk_to_null(session):
    """No pre-delete UPDATE: the FK SET NULLs the rows as history."""
    _case(session, "case-1")
    _investigation(session, "inv-live", "case-1", "executing")

    case_records_service.purge_all_cases(session)
    session.commit()

    assert session.get(Investigation, "inv-live").case_id is None


def test_workdir_sidecar_is_marked_failed(tmp_path, monkeypatch):
    from core.config import get_settings
    from services.api.routers.cases import _mark_workdirs_failed

    monkeypatch.setenv("ORCHESTRATOR_WORKDIR", str(tmp_path))
    get_settings.cache_clear()

    workdir = tmp_path / "inv-live"
    workdir.mkdir()
    (workdir / "state.json").write_text(
        json.dumps({"status": "executing", "step": 2}), encoding="utf-8"
    )

    _mark_workdirs_failed(["inv-live", "inv-missing"], "killed: case reset")

    state = json.loads((workdir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["failure_reason"] == "killed: case reset"
    assert state["step"] == 2
