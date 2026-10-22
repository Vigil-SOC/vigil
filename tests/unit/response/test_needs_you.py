"""Home's queue: pending approvals that need a person, oldest first."""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.v1.approvals_router import router as approvals_router
from core.findings.alert_outcomes import finding_states
from core.response.approval_service import _row_to_pending
from core.storage.models import (
    ApprovalAction,
    Case,
    Finding,
    IntakeTrigger,
    Investigation,
    WorkflowRun,
    case_findings,
)
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
                    reason="Paused at the isolate step",
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
                    reason="Credential dumping is live.; approval.human_only=True",
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
    assert "ny-intake-should-not-appear" not in {
        item["title"] for item in body["items"]
    }
    assert all("expires_at" not in item for item in mine)

    direct, from_run, from_inv, none = mine
    assert direct["kind"] == "checkpoint"
    assert direct["case_id"] == "ny-direct-case"
    assert from_run["kind"] == "approval"
    assert from_run["case_id"] == "ny-from-run"
    assert from_inv["case_id"] == "ny-case-inv"
    assert none["case_id"] is None
    # The ledger's rule tail is not served; a checkpoint's reason is as stored.
    assert from_run["reason"] == "Credential dumping is live."
    assert direct["reason"] == "Paused at the isolate step"
    assert none["reason"] == "because"
    assert {item["source_id"] for item in mine}.isdisjoint({"ny-approved", "ny-auto"})
    # The column is naive UTC; a zoneless string reads as local time in a browser.
    assert direct["created_at"] == "2099-01-01T08:00:00+00:00"


def test_row_to_pending_stamps_every_time_as_utc():
    row = _approval(
        "ny-row",
        OLDER,
        status="executed",
        approved_at=OLDER + timedelta(minutes=1),
        executed_at=OLDER + timedelta(minutes=2),
    )
    pending = _row_to_pending(row)
    assert pending.created_at == "2099-01-01T08:00:00+00:00"
    assert pending.approved_at == "2099-01-01T08:01:00+00:00"
    assert pending.executed_at == "2099-01-01T08:02:00+00:00"


def test_case_id_filter_uses_the_resolved_id(client, seeded):
    direct = _mine(
        client.get(
            "/api/approvals/needs-you", params={"case_id": "ny-direct-case"}
        ).json()
    )
    assert [item["source_id"] for item in direct] == ["ny-direct"]

    from_run = _mine(
        client.get("/api/approvals/needs-you", params={"case_id": "ny-from-run"}).json()
    )
    assert [item["source_id"] for item in from_run] == ["ny-from-run"]

    from_inv = _mine(
        client.get("/api/approvals/needs-you", params={"case_id": "ny-case-inv"}).json()
    )
    assert [item["source_id"] for item in from_inv] == ["ny-from-inv"]


@pytest.fixture
def hunts(throwaway_database):
    """Hunt runs paused for different reasons, each on its own case."""

    def run(run_id: str, reason: str, run_kind: str = "hunt", status: str = "paused"):
        return WorkflowRun(
            run_id=run_id,
            workflow_id="threat-hunt",
            workflow_name="threat-hunt",
            status=status,
            reason=reason,
            trigger_context={"case_id": f"case-{run_id}", "run_kind": run_kind},
            started_at=OLDER,
        )

    with unit_of_work() as session:
        session.add_all(
            [
                run("ny-capped", "ran out of turns: iteration 48 of 48, spent $0.12"),
                run("ny-gated", "Waiting for your approval: Approve and start this"),
                run("ny-awaiting", "awaiting verdict_review checkpoint cp-1: Mark"),
                run("ny-answered", "ran out of turns: iteration 3 of 3"),
                run("ny-compose", "phase 2 waits", run_kind="compose"),
                run("ny-running", "", status="running"),
            ]
        )
        session.flush()
        session.add_all(
            [
                _approval(
                    "ny-gate",
                    OLDER,
                    title="Approve and start this hunt on 2 explanations",
                    workflow_run_id="ny-gated",
                    parameters={
                        "checkpoint_id": "cp-gate",
                        "checkpoint_class": "hypothesis_approval",
                        "hypotheses": [
                            {
                                "hypothesis_id": "h-1",
                                "statement": "FIN-WS-114 beacons",
                                "provenance": "operator",
                            },
                            {
                                "hypothesis_id": "h-0",
                                "statement": "it is backup traffic",
                                "provenance": "base_rate",
                            },
                        ],
                    },
                ),
                # An approval still pending on the run makes it that approval's.
                _approval(
                    "ny-other",
                    OLDER,
                    workflow_run_id="ny-answered",
                    parameters={"checkpoint_id": "cp-2"},
                ),
            ]
        )
    yield
    with unit_of_work() as session:
        session.query(ApprovalAction).filter(
            ApprovalAction.action_id.in_(["ny-gate", "ny-other"])
        ).delete(synchronize_session=False)
        session.query(WorkflowRun).filter(WorkflowRun.run_id.like("ny-%")).delete(
            synchronize_session=False
        )


def test_a_hunt_stopped_at_a_limit_needs_a_person(client, hunts):
    body = client.get("/api/approvals/needs-you").json()
    parked = [item for item in body["items"] if item["kind"] == "parked"]

    assert [item["source_id"] for item in parked] == ["ny-capped"]
    [capped] = parked
    assert capped["case_id"] == "case-ny-capped"
    assert capped["run_id"] == "ny-capped"
    assert capped["title"] == "Keep the hunt going?"
    assert capped["reason"] == "Ran out of turns: iteration 48 of 48, spent $0.12"
    # The case list and Home count it like any other item.
    assert body["count"] == len(body["items"])

    one_case = client.get(
        "/api/approvals/needs-you", params={"case_id": "case-ny-capped"}
    ).json()
    assert [item["source_id"] for item in one_case["items"]] == ["ny-capped"]


@pytest.fixture
def capped_finding(hunts):
    with unit_of_work() as session:
        session.add_all(
            [
                Case(case_id="case-ny-capped", title="capped", status="open"),
                Finding(finding_id="ny-capped-finding", data_source="ny-src"),
            ]
        )
        session.flush()
        session.execute(
            case_findings.insert().values(
                case_id="case-ny-capped", finding_id="ny-capped-finding"
            )
        )
    yield
    with unit_of_work() as session:
        session.execute(
            case_findings.delete().where(
                case_findings.c.finding_id == "ny-capped-finding"
            )
        )
        session.query(Finding).filter(Finding.finding_id == "ny-capped-finding").delete(
            synchronize_session=False
        )
        session.query(Case).filter(Case.case_id == "case-ny-capped").delete(
            synchronize_session=False
        )


def test_a_finding_on_a_hunt_stopped_at_a_limit_needs_you(capped_finding):
    assert finding_states(["ny-capped-finding"]) == {
        "ny-capped-finding": ("needs_you", "case-ny-capped")
    }


def test_a_hypothesis_approval_carries_what_is_being_approved(client, hunts):
    body = client.get(
        "/api/approvals/needs-you", params={"case_id": "case-ny-gated"}
    ).json()
    [gate] = body["items"]
    assert gate["kind"] == "checkpoint"
    assert gate["checkpoint_id"] == "cp-gate"
    assert gate["checkpoint_class"] == "hypothesis_approval"
    assert gate["run_id"] == "ny-gated"
    assert [(h["statement"], h["provenance"]) for h in gate["hypotheses"]] == [
        ("FIN-WS-114 beacons", "operator"),
        ("it is backup traffic", "base_rate"),
    ]
