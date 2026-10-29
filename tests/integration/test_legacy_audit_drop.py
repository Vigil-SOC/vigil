"""The old audit stores dropped by ``python -m core.upgrade`` (#3310).

Each scratch database is seeded before the copy revision and upgraded, as in
``test_legacy_audit_copy.py``, so the copy passed and the stamp is set. The
stamp is then backdated, or not, and the command run again. After a drop the
readers of the three tables are driven through their real API routes.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from core.audit import trail
from core.auth.current_user import get_current_user
from core.storage.connection import get_db_manager
from core.storage.models import User
from core.upgrade import command
from tests.integration.test_legacy_audit_copy import _SEED, BEFORE, _migrate_to
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]

TABLES = set(command.LEGACY_AUDIT_TABLES)
BACKDATE = "UPDATE audit_legacy_copy SET read_only_since = now() - interval '7 months'"


@pytest.fixture
def stamped(tmp_path):
    """A database whose copy passed and whose stamp was set just now."""
    with _scratch(False) as engine:
        _migrate_to(engine, BEFORE)
        with engine.begin() as conn:
            for statement in _SEED.split(";"):
                if statement.strip():
                    conn.execute(text(statement))
        code, report = _run(engine, tmp_path, restore_point="rp-1")
        assert code == command.EXIT_OK, report
        assert report["legacy_audit_read_only"]["set"], report
        yield engine


def _sql(engine, *statements: str) -> None:
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _left(engine) -> set[str]:
    with engine.connect() as conn:
        return {t for t in TABLES if command._present(conn, t)}


def test_a_young_stamp_is_not_dropped_and_says_when(stamped, tmp_path):
    code, report = _run(stamped, tmp_path)
    drop = report["legacy_audit_drop"]
    assert code == command.EXIT_OK and not drop["dropped"], drop
    assert drop["eligible_at"] and drop["reason"].startswith("not eligible until ")
    assert _left(stamped) == TABLES


def test_a_failed_reconciliation_is_not_dropped(stamped, tmp_path):
    _sql(stamped, BACKDATE, "UPDATE audit_legacy_copy SET passed = false")
    drop = _run(stamped, tmp_path)[1]["legacy_audit_drop"]
    assert drop == {"dropped": False, "reason": "the reconciliation did not pass"}
    assert _left(stamped) == TABLES


def test_a_row_with_no_copy_blocks_the_drop(stamped, tmp_path):
    # Written by an old pod after the copy ran.
    _sql(
        stamped,
        BACKDATE,
        "INSERT INTO case_audit_logs (audit_id, entity_type, entity_id, action, "
        "changed_by) "
        "VALUES (99, 'case', 'CASE-9', 'update', 'old-pod')",
    )
    drop = _run(stamped, tmp_path)[1]["legacy_audit_drop"]
    assert not drop["dropped"] and drop["uncopied"] == {"case_audit_logs": 1}
    assert "case_audit_logs" in drop["reason"]
    assert _left(stamped) == TABLES


@pytest.fixture(scope="module")
def dropped(tmp_path_factory):
    """A database whose old stores were dropped, served by the app's manager."""
    with _scratch(False) as engine:
        _migrate_to(engine, BEFORE)
        with engine.begin() as conn:
            for statement in _SEED.split(";"):
                if statement.strip():
                    conn.execute(text(statement))
        tmp = tmp_path_factory.mktemp("up")
        assert _run(engine, tmp, restore_point="rp-1")[0] == command.EXIT_OK
        _sql(engine, BACKDATE)
        code, report = _run(engine, tmp)
        manager = get_db_manager()
        saved = manager._engine, manager._session_factory
        manager._engine = engine
        manager._session_factory = sessionmaker(bind=engine)
        try:
            yield engine, code, report, tmp
        finally:
            manager._engine, manager._session_factory = saved


def test_an_earned_drop_is_on_the_trail_and_runs_once(dropped):
    engine, code, report, tmp = dropped
    assert (code, report["outcome"]) == (command.EXIT_OK, "restart")
    assert report["legacy_audit_drop"] == {
        "dropped": True,
        "tables": {
            "case_audit_logs": 3,
            "config_audit_log": 3,
            "investigation_logs": 1,
        },
    }
    assert _left(engine) == set()
    with engine.connect() as conn:
        assert trail.verify(conn).ok
        (entry,) = conn.execute(
            text("SELECT payload FROM audit_trail WHERE payload->>'action' = :a"),
            {"a": command.ACTION_LEGACY_DROP},
        ).scalars()
        # Nothing but the three tables and their view went.
        assert command._present(conn, "investigations")
        assert command._present(conn, "audit_legacy_copy")
    assert entry["actor"] == trail.SYSTEM and entry["tables"]["case_audit_logs"] == 3

    again = _run(engine, tmp)[1]["legacy_audit_drop"]
    assert not again["dropped"] and again["gone"], again


def test_the_upgrade_check_reports_no_drift_after_the_drop(dropped):
    code, report = _run(dropped[0], dropped[3], check=True)
    assert code == command.EXIT_OK and report["drift"] == [], report["drift"]


def test_the_startup_schema_check_is_ok_after_the_drop(dropped):
    report = get_db_manager().schema_report()
    assert report["state"] == "ok", report


@pytest.fixture
def client(dropped, tmp_path, monkeypatch):
    from services.api.routers import cases, orchestrator

    # The daemon purge wipes its workdir.
    monkeypatch.setenv("ORCHESTRATOR_WORKDIR", str(tmp_path / "investigations"))
    monkeypatch.setenv("DEMO_MODE", "false")

    _sql(
        dropped[0],
        "INSERT INTO cases (case_id, title, status, priority, timeline) "
        "VALUES ('CASE-1', 'One', 'open', 'low', '[]') ON CONFLICT DO NOTHING",
    )
    app = FastAPI()
    app.include_router(cases.router, prefix="/api/cases")
    app.include_router(orchestrator.router, prefix="/api/orchestrator")
    app.dependency_overrides[get_current_user] = lambda: User(
        user_id="system", username="admin", role_id="role-admin", is_active=True
    )
    with TestClient(app) as c:
        yield c


def test_the_readers_work_without_the_tables(dropped, client, monkeypatch):
    from core.auth import permissions

    monkeypatch.setattr(permissions, "missing_permission", lambda *a, **k: None)

    record = client.get("/api/cases/CASE-1/record")
    assert record.status_code == 200, record.text

    custody = client.get("/api/orchestrator/investigations/INV-1/chain-of-custody")
    assert custody.status_code == 200, custody.text
    assert custody.json()["logs"] == []

    purge = client.delete("/api/cases/all")
    assert purge.status_code == 200, purge.text

    # INV-1 kept its log rows before the drop; now nothing holds it.
    from services.daemon.config import DaemonConfig
    from services.daemon.orchestrator import Orchestrator

    orch = Orchestrator(DaemonConfig.from_env().orchestrator)
    assert asyncio.run(orch.purge_all_investigations())["deleted"] == 1
