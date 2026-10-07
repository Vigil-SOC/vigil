"""Turning a workflow off, and every place that starts or chooses one (#1623)."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.api.v1 import agent_runs_router
from core.storage.models import ConfigAuditLog, SystemConfig
from core.workflows import catalog, workflows_router
from core.workflows.enablement import (
    DISABLED_WORKFLOWS_KEY,
    disabled_workflow_ids,
    is_enabled,
    set_workflow_enabled,
)
from core.workflows.routing import (
    FALLBACK_WORKFLOW,
    ROUTED_WORKFLOWS,
    SCHEDULED_WORKFLOW,
    SHADOW_WORKFLOW_ID,
)
from core.workflows.workflows_service import WorkflowDefinition, WorkflowsService
from services.daemon.config import SchedulerConfig
from services.daemon.plan_generator import select_workflow
from services.daemon.scheduler import TaskScheduler

pytestmark = pytest.mark.unit

CUSTOM = "custom-hunt"


class _Query:
    def __init__(self, rows, model):
        self._rows, self._model, self._filters = rows, model, {}

    def filter_by(self, **kwargs):
        self._filters = kwargs
        return self

    def first(self):
        for row in self._rows:
            if type(row) is self._model and all(
                getattr(row, k) == v for k, v in self._filters.items()
            ):
                return row
        return None


class _Session:
    def __init__(self):
        self.rows: list = []

    def query(self, model):
        return _Query(self.rows, model)

    def add(self, obj):
        if obj not in self.rows:
            self.rows.append(obj)

    def commit(self):
        return None


@pytest.fixture
def session(monkeypatch):
    s = _Session()

    @contextmanager
    def fake_session():
        yield s

    monkeypatch.setattr("core.storage.config_service.get_session", fake_session)
    return s


def _definition(workflow_id):
    return WorkflowDefinition(
        workflow_id=workflow_id,
        file_path=None,
        metadata={"name": workflow_id, "description": "", "phases": []},
        body="",
        source="file",
    )


@pytest.fixture
def service(monkeypatch):
    svc = WorkflowsService()
    known = {FALLBACK_WORKFLOW, SCHEDULED_WORKFLOW, SHADOW_WORKFLOW_ID, CUSTOM}
    monkeypatch.setattr(
        svc, "get_workflow", lambda wid: _definition(wid) if wid in known else None
    )
    monkeypatch.setattr(
        svc, "list_workflows", lambda: [{"id": w} for w in sorted(known)]
    )
    monkeypatch.setattr(catalog, "_week_run_stats", lambda now: {})
    return svc


@pytest.fixture
def client(authenticate_app, session, service):
    app = FastAPI()
    app.include_router(workflows_router.router, prefix="/api")
    app.state.workflows = service
    authenticate_app(app)
    return TestClient(app)


def _row(client, workflow_id):
    rows = client.get("/api/workflows").json()["workflows"]
    return next(r for r in rows if r["id"] == workflow_id)


def _disable(session, *ids):
    session.rows.append(
        SystemConfig(
            key=DISABLED_WORKFLOWS_KEY,
            value={"ids": list(ids)},
            config_type="workflows",
        )
    )


@pytest.mark.parametrize("workflow_id", [SCHEDULED_WORKFLOW, CUSTOM])
def test_toggle_round_trip_for_a_built_in_and_a_custom_id(client, session, workflow_id):
    assert _row(client, workflow_id)["enabled"] is True

    off = client.put(f"/api/workflows/{workflow_id}/enabled", json={"enabled": False})
    assert off.status_code == 200, off.text
    assert _row(client, workflow_id)["enabled"] is False
    assert workflow_id in disabled_workflow_ids()
    # The shadow stays off beside it: a row replaces the default, it does not drop it.
    assert SHADOW_WORKFLOW_ID in disabled_workflow_ids()

    client.put(f"/api/workflows/{workflow_id}/enabled", json={"enabled": True})
    assert _row(client, workflow_id)["enabled"] is True
    assert workflow_id not in disabled_workflow_ids()


def test_toggle_is_audited_as_the_user(client, session):
    client.put(f"/api/workflows/{SCHEDULED_WORKFLOW}/enabled", json={"enabled": False})
    audit = [r for r in session.rows if isinstance(r, ConfigAuditLog)]
    assert [a.config_key for a in audit] == [DISABLED_WORKFLOWS_KEY]
    assert audit[0].changed_by == "test-admin"
    assert SCHEDULED_WORKFLOW in audit[0].change_reason
    assert SCHEDULED_WORKFLOW in audit[0].new_value["ids"]


def test_the_fallback_refuses_to_turn_off_and_says_why(client, session):
    r = client.put(
        f"/api/workflows/{FALLBACK_WORKFLOW}/enabled", json={"enabled": False}
    )
    assert r.status_code == 409
    assert "cannot be turned off" in r.json()["detail"]
    assert session.rows == []
    assert _row(client, FALLBACK_WORKFLOW)["can_disable"] is False
    assert _row(client, SCHEDULED_WORKFLOW)["can_disable"] is True


def test_unknown_workflow_is_404_and_failed_write_is_500(client, monkeypatch):
    assert (
        client.put("/api/workflows/nope/enabled", json={"enabled": False}).status_code
        == 404
    )
    monkeypatch.setattr(workflows_router, "set_workflow_enabled", lambda *a: False)
    r = client.put(f"/api/workflows/{CUSTOM}/enabled", json={"enabled": False})
    assert r.status_code == 500


def test_the_shadow_is_off_with_no_row_and_can_be_turned_on_and_off(client, session):
    assert disabled_workflow_ids() == {SHADOW_WORKFLOW_ID}
    assert _row(client, SHADOW_WORKFLOW_ID)["enabled"] is False

    on = client.put(
        f"/api/workflows/{SHADOW_WORKFLOW_ID}/enabled", json={"enabled": True}
    )
    assert on.status_code == 200
    # An empty list is a row, and a row replaces the default.
    assert disabled_workflow_ids() == set()
    assert _row(client, SHADOW_WORKFLOW_ID)["enabled"] is True

    client.put(f"/api/workflows/{SHADOW_WORKFLOW_ID}/enabled", json={"enabled": False})
    assert disabled_workflow_ids() == {SHADOW_WORKFLOW_ID}


def test_a_failed_read_is_the_default(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("core.storage.config_service.get_session", boom)
    assert disabled_workflow_ids() == {SHADOW_WORKFLOW_ID}


def test_listing_carries_triggers_can_disable_and_enabled(client):
    rows = {r["id"]: r for r in client.get("/api/workflows").json()["workflows"]}
    assert rows[FALLBACK_WORKFLOW]["triggers"] == ["alerts"]
    assert rows[SCHEDULED_WORKFLOW]["triggers"] == ["schedule"]
    assert rows[SHADOW_WORKFLOW_ID]["triggers"] == ["shadow"]
    assert rows[CUSTOM]["triggers"] == []


@pytest.mark.parametrize(
    "finding",
    [
        {"severity": "critical"},
        {"recommended_action": "block"},
        {"category": "malware"},
        {"category": "ransomware"},
        {"mitre_predictions": {"T1": 1, "T2": 1, "T3": 1}},
        {"severity": "high"},
        {"severity": "low"},
        {},
    ],
)
def test_select_workflow_returns_only_routed_workflows(finding):
    assert select_workflow(finding) in ROUTED_WORKFLOWS


# --- where a run starts -------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_refuses_a_disabled_workflow_before_a_run_row(
    client, session, monkeypatch
):
    _disable(session, SCHEDULED_WORKFLOW)
    begin = MagicMock()
    monkeypatch.setattr(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run", begin
    )
    r = client.post(
        f"/api/workflows/{SCHEDULED_WORKFLOW}/execute", json={"hypothesis": "x"}
    )
    assert r.status_code == 409
    assert SCHEDULED_WORKFLOW in r.json()["detail"]
    begin.assert_not_called()


@pytest.mark.asyncio
async def test_the_service_refuses_for_every_caller(session, service):
    _disable(session, CUSTOM)
    result = await service.execute_workflow(CUSTOM, {})
    assert result["success"] is False and result["disabled"] is True


def test_execute_accepts_an_enabled_workflow(client, service):
    with patch.object(
        WorkflowsService,
        "execute_workflow",
        new=AsyncMock(return_value={"success": True, "run_id": "r1"}),
    ):
        r = client.post(
            f"/api/workflows/{SCHEDULED_WORKFLOW}/execute", json={"hypothesis": "x"}
        )
    assert r.status_code == 200


def _start(playbook):
    return agent_runs_router.StartRunRequest(
        run_kind="hunt", playbook=playbook, config="c"
    )


@pytest.mark.asyncio
async def test_v1_start_refuses_a_disabled_workflow_and_accepts_an_enabled_one(session):
    _disable(session, SCHEDULED_WORKFLOW)
    with pytest.raises(HTTPException) as exc:
        await agent_runs_router.start_run(_start(f"workflow:{SCHEDULED_WORKFLOW}"))
    assert exc.value.status_code == 409
    assert SCHEDULED_WORKFLOW in exc.value.detail

    with (
        patch.object(agent_runs_router, "_begin_run_row"),
        patch.object(agent_runs_router, "enqueue_run", AsyncMock(return_value="j")),
    ):
        res = await agent_runs_router.start_run(_start(f"workflow:{CUSTOM}"))
    assert res.job_id == "j"


# --- the daemon ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_routes_a_disabled_workflow_to_the_fallback(session):
    from services.daemon.orchestrator import Orchestrator

    _disable(session, "full-investigation")
    orch = object.__new__(Orchestrator)
    orch.shared_intel = MagicMock()
    orch.shared_intel.check_overlap.return_value = None
    orch._create_investigation = AsyncMock()
    finding = {"finding_id": "f-1", "severity": "high"}
    assert select_workflow(finding) == "full-investigation"

    await orch._create_investigation_for_finding(finding, None)
    assert (
        orch._create_investigation.await_args.kwargs["workflow_id"] == FALLBACK_WORKFLOW
    )

    session.rows.clear()
    await orch._create_investigation_for_finding(finding, None)
    assert (
        orch._create_investigation.await_args.kwargs["workflow_id"]
        == "full-investigation"
    )


@pytest.mark.asyncio
async def test_scheduler_skips_the_hunt_while_it_is_off(session, monkeypatch):
    rows = []
    monkeypatch.setattr(
        "services.daemon.orchestrator.insert_intake_trigger",
        lambda **kw: rows.append(kw),
    )
    scheduler = TaskScheduler(SchedulerConfig())
    scheduler._data_service = None

    _disable(session, SCHEDULED_WORKFLOW)
    await scheduler._run_threat_hunt()
    assert rows == [] and scheduler.stats["threat_hunts"] == 0

    session.rows.clear()
    await scheduler._run_threat_hunt()
    assert len(rows) == 1


def test_the_hunt_task_is_always_registered():
    scheduler = TaskScheduler(SchedulerConfig())
    assert "threat_hunt" in [t.name for t in scheduler._tasks]


def test_the_feed_poller_offers_no_hunt_while_it_is_off(session):
    from services.daemon.threat_feed_poller import ThreatFeedPoller

    _disable(session, SCHEDULED_WORKFLOW)
    poller = object.__new__(ThreatFeedPoller)
    assert poller.offer_uncovered_indicators_to_intake() == {
        "inserted": 0,
        "skipped": "workflow_disabled",
    }


def test_setting_the_current_state_writes_nothing(session):
    assert set_workflow_enabled(SCHEDULED_WORKFLOW, True, "u") is True
    assert session.rows == []
    assert is_enabled(SCHEDULED_WORKFLOW)
