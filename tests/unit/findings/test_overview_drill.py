"""Overview drill-down: the alerts behind an outcome node, and one agent card opened."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.findings.overview_drill import agent_detail, outcome_alerts
from core.storage.models import Case, Finding, WorkflowRun
from core.storage.models.base import case_findings
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow
from core.workflows.workflows_service import WorkflowsService

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _now():
    return utcnow().replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _clean(throwaway_database):
    def purge():
        with unit_of_work() as session:
            session.execute(
                case_findings.delete().where(case_findings.c.finding_id.like("od-%"))
            )
            session.query(Case).filter(Case.case_id.like("od-%")).delete(
                synchronize_session=False
            )
            session.query(Finding).filter(Finding.finding_id.like("od-%")).delete(
                synchronize_session=False
            )
            session.query(WorkflowRun).filter(WorkflowRun.run_id.like("od-%")).delete(
                synchronize_session=False
            )

    purge()
    yield
    purge()


def _finding(finding_id, minutes_ago, **kw):
    with unit_of_work() as session:
        session.add(
            Finding(
                finding_id=finding_id,
                data_source=kw.get("data_source", "od-edr"),
                severity="high",
                title=finding_id,
                noise_marked_at=kw.get("noise_marked_at"),
                noise_marked_by_kind=kw.get("noise_marked_by_kind"),
                created_at=_now() - timedelta(minutes=minutes_ago),
            )
        )


def _ids(body):
    return [
        item["finding_id"]
        for item in body["items"]
        if item["finding_id"].startswith("od-")
    ]


def test_an_outcome_lists_its_alerts_newest_first_with_their_cases():
    _finding("od-1", 1)
    _finding("od-2", 2)
    _finding("od-3", 3, noise_marked_at=_now(), noise_marked_by_kind="agent")
    with unit_of_work() as session:
        session.add(
            Case(case_id="od-c1", title="Case od-c1", description="", status="open")
        )
        session.flush()
        for fid in ("od-1", "od-2"):
            session.execute(
                case_findings.insert().values(case_id="od-c1", finding_id=fid)
            )

    working = outcome_alerts("working", limit=200)
    assert working["label"] == "Still working"
    assert _ids(working) == ["od-1", "od-2"]
    assert {
        i["case_id"] for i in working["items"] if i["finding_id"].startswith("od-")
    } == {"od-c1"}
    case = next(c for c in working["cases"] if c["case_id"] == "od-c1")
    assert case["alerts"] == 2 and case["title"] == "Case od-c1"
    assert {"data_source": "od-edr", "count": 2} in working["by_source"]

    dropped = outcome_alerts("dismissed", limit=200)
    assert dropped["label"] == "Dropped as noise"
    assert "od-3" in _ids(dropped)
    assert dropped["cases"] == []

    page = outcome_alerts("working", limit=1, offset=1)
    assert page["offset"] == 1 and len(page["items"]) == 1


def test_an_unknown_outcome_is_none():
    assert outcome_alerts("nope") is None


def test_an_agent_card_opens_its_workflow_runs_and_record():
    service = WorkflowsService()
    workflow = service.list_workflows()[0]
    wid = workflow["id"] if isinstance(workflow, dict) else workflow.id
    now = _now()
    with unit_of_work() as session:
        for i, status in enumerate(["completed", "completed", "failed", "running"]):
            session.add(
                WorkflowRun(
                    run_id=f"od-run-{i}",
                    workflow_id=wid,
                    workflow_name=wid,
                    status=status,
                    started_at=now - timedelta(minutes=10 * (i + 1)),
                    duration_ms=1000 * (i + 1),
                    total_cost_usd=0.5,
                    trigger_context={"finding_ids": [f"od-f{i}"]},
                )
            )

    body = agent_detail(wid, service)
    assert body["workflow"]["id"] == wid
    assert "od-run-3" in [r["run_id"] for r in body["live"]]
    recent = {r["run_id"]: r for r in body["recent"]}
    assert recent["od-run-0"]["finding_id"] == "od-f0"
    assert body["record"]["runs"] >= 3
    assert body["record"]["failed"] >= 1
    assert agent_detail("no-such-workflow", service) is None


def test_routes_say_404_for_what_the_board_does_not_have():
    from services.api.routers import overview

    app = FastAPI()
    app.include_router(overview.router, prefix=overview.ROUTER_META.prefix)
    from core.deps import provide_workflows

    app.dependency_overrides[provide_workflows] = lambda: WorkflowsService()
    client = TestClient(app)
    assert client.get("/api/overview/outcomes/nope").status_code == 404
    assert client.get("/api/overview/outcomes/working").status_code == 200
    assert client.get("/api/overview/agents/no-such-workflow").status_code == 404
