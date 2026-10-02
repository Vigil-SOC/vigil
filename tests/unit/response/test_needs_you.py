"""Home's queue: pending approvals that need a person, oldest first."""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.v1.approvals_router import router as approvals_router
from core.storage.models import ApprovalAction, Case, IntakeTrigger, Investigation, WorkflowRun
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

OLDER = datetime(2099, 1, 1, 8, 0, 0)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(approvals_router, prefix="/api")
    return TestClient(app)


def _approval(action_id: str, created_at: datetime, **kw) -> ApprovalAction:
    return ApprovalAction(
        action_id=action_id,
        action_type="block_ip",
        title=kw.pop("title", action_id),
        description="hold",
        target="host",
        confidence=0.4,
        reason=kw.pop("reason", "because"),
        evidence=[],
        created_by="pytest",
        requires_approval=kw.pop("requires_approval", True),
        status=kw.pop("status", "pending"),
        created_at=created_at,
        parameters=kw.pop("parameters", {}),
        **kw,
    )


@pytest.fixture
def seeded(throwaway_database):
    with unit_of_work() as session:
        session.add_all(
            [
                Case(case_id="ny-case-inv", title="inv", status="open"),
                WorkflowRun(
                    run_id="ny-run",
                    workflow_id="ny-wf",
                    workflow_name="Needs you",
                    status="paused",
                    trigger_context={"case_id": "ny-from-run"},
                    started_at=OLDER,
                ),
                Investigation(
                    investigation_id="ny-inv",
                    case_id="ny-case-inv",
                    workflow_id="ny-wf",
                    trigger_type="manual",
                    trigger_ids=[],
                    workdir="/tmp/ny",
                ),
                IntakeTrigger(
                    kind="detection",
                    state="queued",
                    reason="ny-intake-should-not-appear",
                    payload={"case_id": "ny-from-intake"},
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                _approval(
                    "ny-direct",
                    OLDER,
                    title="Direct case",
                    workflow_run_id="ny-run",
                    parameters={
                        "checkpoint_id": "chk-1",
                        "case_id": "ny-direct-case",
                        "investigation_id": "ny-inv",
                    },
                ),
                _approval(
                    "ny-from-run",
                    OLDER + timedelta(hours=1),
                    title="Run case",
                    workflow_run_id="ny-run",
                    parameters={
                        "case_id": "  ",
                        "checkpoint_id": "",
                        "investigation_id": "ny-inv",
                    },
                ),
                _approval(
                    "ny-from-inv",
                    OLDER + timedelta(hours=2),
                    title="Investigation case",
                    parameters={"investigation_id": "ny-inv"},
                ),
                _approval(
                    "ny-none",
                    OLDER + timedelta(hours=3),
                    title="No case",
                    parameters={},
                ),
                _approval(
                    "ny-approved",
                    OLDER - timedelta(hours=1),
                    status="approved",
                    parameters={"case_id": "ny-direct-case"},
                ),
                _approval(
                    "ny-auto",
                    OLDER - timedelta(hours=2),
                    requires_approval=False,
                    parameters={"case_id": "ny-direct-case"},
                ),
            ]
        )
    yield
    with unit_of_work() as session:
        session.query(ApprovalAction).filter(
            ApprovalAction.action_id.like("ny-%")
        ).delete(synchronize_session=False)
        session.query(IntakeTrigger).filter(
            IntakeTrigger.reason == "ny-intake-should-not-appear"
        ).delete(synchronize_session=False)
        session.query(Investigation).filter(
            Investigation.investigation_id == "ny-inv"
        ).delete(synchronize_session=False)
        session.query(WorkflowRun).filter(WorkflowRun.run_id == "ny-run").delete(
            synchronize_session=False
        )
        session.query(Case).filter(Case.case_id == "ny-case-inv").delete(
            synchronize_session=False
        )


def _mine(body: dict) -> list[dict]:
    return [item for item in body["items"] if str(item["source_id"]).startswith("ny-")]


def test_needs_you_is_oldest_pending_approvals_only(client, seeded):
    body = client.get("/api/approvals/needs-you").json()
    mine = _mine(body)

    assert body["count"] == len(body["items"])
    assert [item["source_id"] for item in mine] == [
        "ny-direct",
        "ny-from-run",
        "ny-from-inv",
        "ny-none",
    ]
    assert "ny-intake-should-not-appear" not in {item["title"] for item in body["items"]}
    assert all("expires_at" not in item for item in mine)

    direct, from_run, from_inv, none = mine
    assert direct["kind"] == "checkpoint"
    assert direct["case_id"] == "ny-direct-case"
    assert from_run["kind"] == "approval"
    assert from_run["case_id"] == "ny-from-run"
    assert from_inv["case_id"] == "ny-case-inv"
    assert none["case_id"] is None
    assert {item["source_id"] for item in mine}.isdisjoint({"ny-approved", "ny-auto"})


def test_case_id_filter_uses_the_resolved_id(client, seeded):
    direct = _mine(client.get("/api/approvals/needs-you", params={"case_id": "ny-direct-case"}).json())
    assert [item["source_id"] for item in direct] == ["ny-direct"]

    from_run = _mine(client.get("/api/approvals/needs-you", params={"case_id": "ny-from-run"}).json())
    assert [item["source_id"] for item in from_run] == ["ny-from-run"]

    from_inv = _mine(client.get("/api/approvals/needs-you", params={"case_id": "ny-case-inv"}).json())
    assert [item["source_id"] for item in from_inv] == ["ny-from-inv"]
