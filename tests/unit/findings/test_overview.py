"""Overview arrivals, terminal states, and agent rows."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.findings.alert_outcomes import terminal_states_today
from core.findings.arrival_counts import arrivals_today_by_source
from core.findings.overview import FEED_LIMIT, completion_level, overview_payload
from core.response.approval_service import pending_approval_case_ids
from core.storage.models import (
    ApprovalAction,
    Case,
    CaseClosureInfo,
    CustomWorkflow,
    FederationSource,
    Finding,
    Investigation,
    WorkflowRun,
    WorkflowRunPhase,
    case_findings,
)
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

DAY = datetime(2099, 12, 31)
NOW = datetime(2099, 6, 1, 12, 0, 0)
IN_WINDOW = datetime(2099, 5, 20, 8, 0, 0)


@pytest.fixture(autouse=True)
def _clean(throwaway_database):
    def purge():
        with unit_of_work() as session:
            session.query(ApprovalAction).filter(
                ApprovalAction.action_id.like("ov-%")
            ).delete(synchronize_session=False)
            session.query(WorkflowRunPhase).filter(
                WorkflowRunPhase.run_id.like("ov-%")
            ).delete(synchronize_session=False)
            session.query(WorkflowRun).filter(WorkflowRun.run_id.like("ov-%")).delete(
                synchronize_session=False
            )
            session.query(Investigation).filter(
                Investigation.investigation_id.like("ov-%")
            ).delete(synchronize_session=False)
            session.query(CaseClosureInfo).filter(
                CaseClosureInfo.case_id.like("ov-%")
            ).delete(synchronize_session=False)
            session.execute(
                case_findings.delete().where(case_findings.c.finding_id.like("ov-%"))
            )
            session.query(Case).filter(Case.case_id.like("ov-%")).delete(
                synchronize_session=False
            )
            session.query(Finding).filter(Finding.finding_id.like("ov-%")).delete(
                synchronize_session=False
            )
            session.query(FederationSource).filter(
                FederationSource.source_id.like("ov-%")
            ).delete(synchronize_session=False)
            session.query(CustomWorkflow).filter(
                CustomWorkflow.workflow_id.like("ov-%")
            ).delete(synchronize_session=False)

    purge()
    yield
    purge()


@pytest.fixture
def client():
    from services.api.routers import overview

    app = FastAPI()
    app.include_router(overview.router, prefix=overview.ROUTER_META.prefix)
    return TestClient(app)


def _finding(finding_id: str, at: datetime, **kw) -> Finding:
    return Finding(
        finding_id=finding_id,
        data_source=kw.pop("data_source", "ov-src"),
        created_at=at,
        status="new",
        severity="high",
        **kw,
    )


def _case(case_id: str, status: str, updated_at: datetime) -> Case:
    return Case(case_id=case_id, title=case_id, status=status, updated_at=updated_at)


def _link(session, case_id: str, finding_id: str) -> None:
    session.execute(
        case_findings.insert().values(
            case_id=case_id, finding_id=finding_id, added_at=DAY
        )
    )


def _approval(action_id: str, **kw) -> ApprovalAction:
    return ApprovalAction(
        action_id=action_id,
        action_type="block_ip",
        title="hold",
        description="hold",
        target="host",
        confidence=0.4,
        reason="because",
        evidence=[],
        created_by="pytest",
        requires_approval=True,
        status="pending",
        parameters=kw.pop("parameters", {}),
        **kw,
    )


def test_completion_level_bands():
    assert completion_level(0, 0) is None
    assert completion_level(20, 19) == "good"
    assert completion_level(100, 94) == "fair"
    assert completion_level(20, 17) == "fair"
    assert completion_level(100, 84) == "poor"


def test_terminal_states_partition_the_day():
    inside = DAY + timedelta(hours=3)
    with unit_of_work() as session:
        session.add_all(
            [
                FederationSource(source_id="ov-quiet", enabled=True),
                FederationSource(source_id="ov-off", enabled=False),
                _case("ov-c-you", "closed", inside),
                _case("ov-c-run", "open", inside),
                _case("ov-c-inv", "open", inside),
                _case("ov-c-other", "open", inside),
                _case("ov-c-auto", "closed", inside),
                _case("ov-c-person", "closed", inside),
                _case("ov-c-work", "open", inside),
                _case("ov-c-old", "open", datetime(2099, 1, 1)),
                _case("ov-c-new", "open", datetime(2099, 6, 1)),
                _finding(
                    "ov-f-you",
                    inside,
                    description="needs a person",
                    evidence_links=[{"ref": "https://example.test/you"}],
                    entity_context={
                        "source_evidence": {
                            "version": 1,
                            "telemetry_kind": "dns",
                            "schema_id": "dns.v1",
                            "status": "available",
                            "provenance": "embedded",
                            "records": [{"query": "evil.example"}],
                            "total_records": 1,
                        }
                    },
                ),
                _finding("ov-f-run", inside),
                _finding("ov-f-inv", inside),
                _finding("ov-f-other", inside),
                _finding("ov-f-auto", inside),
                _finding("ov-f-person", inside),
                _finding("ov-f-work", inside),
                _finding("ov-f-latest", inside),
                _finding("ov-f-wait", inside),
                _finding("ov-f-start", DAY),
                _finding("ov-f-before", DAY - timedelta(seconds=1)),
                _finding("ov-f-after", DAY + timedelta(days=1)),
            ]
        )
        session.flush()
        session.add_all(
            [
                CaseClosureInfo(
                    case_id="ov-c-you",
                    closure_category="resolved",
                    closed_by="ada",
                    closed_by_kind="analyst",
                ),
                CaseClosureInfo(
                    case_id="ov-c-person",
                    closure_category="resolved",
                    closed_by="ada",
                    closed_by_kind="analyst",
                ),
                WorkflowRun(
                    run_id="ov-run-ctx",
                    workflow_id="ov-rate",
                    workflow_name="Rate",
                    status="paused",
                    trigger_context={"case_id": "ov-c-other"},
                    started_at=IN_WINDOW,
                ),
                WorkflowRun(
                    run_id="ov-run-only",
                    workflow_id="ov-rate",
                    workflow_name="Rate",
                    status="completed",
                    trigger_context={"case_id": "ov-c-run"},
                    started_at=IN_WINDOW,
                ),
                Investigation(
                    investigation_id="ov-inv",
                    case_id="ov-c-inv",
                    workflow_id="ov-rate",
                    trigger_type="manual",
                    trigger_ids=[],
                    workdir="/tmp/ov",
                ),
            ]
        )
        session.flush()
        for case_id, finding_id in (
            ("ov-c-you", "ov-f-you"),
            ("ov-c-run", "ov-f-run"),
            ("ov-c-inv", "ov-f-inv"),
            ("ov-c-other", "ov-f-other"),
            ("ov-c-auto", "ov-f-auto"),
            ("ov-c-person", "ov-f-person"),
            ("ov-c-work", "ov-f-work"),
            ("ov-c-old", "ov-f-latest"),
            ("ov-c-new", "ov-f-latest"),
        ):
            _link(session, case_id, finding_id)
        session.add_all(
            [
                # parameters.case_id wins over the run's different case.
                _approval(
                    "ov-a-you",
                    parameters={"case_id": "ov-c-you"},
                    workflow_run_id="ov-run-ctx",
                ),
                _approval("ov-a-run", workflow_run_id="ov-run-only"),
                _approval("ov-a-inv", parameters={"investigation_id": "ov-inv"}),
                _approval("ov-a-old", parameters={"case_id": "ov-c-old"}),
            ]
        )

    states = {
        row["finding_id"]: row["terminal_state"]
        for row in terminal_states_today(DAY.date())
        if row["finding_id"].startswith("ov-")
    }
    assert states == {
        "ov-f-you": "needs_you",
        "ov-f-run": "needs_you",
        "ov-f-inv": "needs_you",
        "ov-f-other": "working",
        "ov-f-auto": "resolved_auto",
        "ov-f-person": "resolved_person",
        "ov-f-work": "working",
        "ov-f-latest": "working",
        "ov-f-wait": "waiting",
        "ov-f-start": "waiting",
    }
    assert "ov-f-before" not in states
    assert "ov-f-after" not in states

    arrivals = {
        row["data_source"]: row["count"] for row in arrivals_today_by_source(DAY.date())
    }
    assert arrivals["ov-src"] == len(states)
    assert arrivals["ov-quiet"] == 0
    assert "ov-off" not in arrivals
    assert sum(arrivals.values()) == len(terminal_states_today(DAY.date()))

    pending = pending_approval_case_ids()
    assert "ov-c-you" in pending
    assert "ov-c-run" in pending
    assert "ov-c-inv" in pending
    assert "ov-c-other" not in pending

    payload = overview_payload(day=DAY.date(), now=NOW)
    live = [node["count"] for node in payload["outcomes"] if node["count"] is not None]
    assert sum(live) == sum(row["count"] for row in payload["arrivals"])
    unmeasured = {
        node["state"]: node
        for node in payload["outcomes"]
        if node["state"] in {"ticket", "dropped", "paused", "stuck", "incidents"}
    }
    assert set(unmeasured) == {"ticket", "dropped", "paused", "stuck", "incidents"}
    for node in unmeasured.values():
        assert node["count"] is None
        assert node["unmeasured_text"] == "Not measured yet"
    needs = next(node for node in payload["outcomes"] if node["state"] == "needs_you")
    assert "alerts" in needs["info"]
    assert "No count" in payload["engine"]["source_text"]
    assert "count" not in payload["engine"]
    assert payload["empty"] is False

    item = next(row for row in payload["feed"] if row["finding_id"] == "ov-f-you")
    assert item["status"] == "new"
    assert item["terminal_state"] == "needs_you"
    assert item["terminal_label"] == "Needs you"
    assert item["evidence_links"] == [{"ref": "https://example.test/you"}]
    assert "records" not in item["source_evidence"]
    assert item["source_evidence"]["payload_included"] is False


def test_rate_includes_deleted_rows_and_phase_falls_back():
    with unit_of_work() as session:
        session.add_all(
            [
                CustomWorkflow(
                    workflow_id="ov-rate", name="OV Rate", description="rate"
                ),
                CustomWorkflow(
                    workflow_id="ov-idle", name="OV Idle", description="idle"
                ),
                CustomWorkflow(
                    workflow_id="ov-phase", name="OV Phase", description="phase"
                ),
                WorkflowRun(
                    run_id="ov-done",
                    workflow_id="ov-rate",
                    workflow_name="OV Rate",
                    status="completed",
                    started_at=IN_WINDOW,
                ),
                WorkflowRun(
                    run_id="ov-failed",
                    workflow_id="ov-rate",
                    workflow_name="OV Rate",
                    status="failed",
                    started_at=IN_WINDOW,
                    deleted_at=IN_WINDOW,
                ),
                WorkflowRun(
                    run_id="ov-hidden-live",
                    workflow_id="ov-rate",
                    workflow_name="OV Rate",
                    status="running",
                    started_at=IN_WINDOW,
                    deleted_at=IN_WINDOW,
                ),
                WorkflowRun(
                    run_id="ov-live-old",
                    workflow_id="ov-rate",
                    workflow_name="OV Rate",
                    status="running",
                    started_at=IN_WINDOW - timedelta(hours=2),
                ),
                WorkflowRun(
                    run_id="ov-live-new",
                    workflow_id="ov-rate",
                    workflow_name="OV Rate",
                    status="paused",
                    started_at=IN_WINDOW,
                ),
                WorkflowRunPhase(
                    run_id="ov-live-old",
                    phase_id="triage",
                    phase_order=1,
                    agent_id="triage",
                    status="running",
                ),
                WorkflowRun(
                    run_id="ov-phased",
                    workflow_id="ov-phase",
                    workflow_name="OV Phase",
                    status="running",
                    started_at=IN_WINDOW,
                ),
                WorkflowRunPhase(
                    run_id="ov-phased",
                    phase_id="collect",
                    phase_order=1,
                    agent_id="collector",
                    status="running",
                ),
                WorkflowRunPhase(
                    run_id="ov-phased",
                    phase_id="contain",
                    phase_order=4,
                    agent_id="responder",
                    status="pending_approval",
                ),
                WorkflowRunPhase(
                    run_id="ov-phased",
                    phase_id="done",
                    phase_order=9,
                    agent_id="responder",
                    status="completed",
                ),
            ]
        )

    payload = overview_payload(day=DAY.date(), now=NOW)
    by_id = {row["workflow_id"]: row for row in payload["agents"]}
    rate = by_id["ov-rate"]
    assert rate["sample_size"] == 2
    assert rate["rate"] == 0.5
    assert rate["level"] == "poor"
    assert rate["running"] == 2
    assert rate["current_step"] == "paused"
    idle = by_id["ov-idle"]
    assert idle["sample_size"] == 0
    assert idle["rate"] is None
    assert idle["level"] is None
    assert idle["running"] == 0
    assert idle["current_step"] is None
    assert by_id["ov-phase"]["current_step"] == "contain"
    assert "budget" in payload["rate_info"]
    assert "completed" in payload["rate_info"]


def test_empty_when_no_source_is_enabled_and_nothing_arrived():
    enabled: list[str] = []
    with unit_of_work() as session:
        enabled = [
            row.source_id
            for row in session.query(FederationSource).filter(
                FederationSource.enabled.is_(True)
            )
        ]
        if enabled:
            session.query(FederationSource).filter(
                FederationSource.source_id.in_(enabled)
            ).update({FederationSource.enabled: False}, synchronize_session=False)
    try:
        payload = overview_payload(day=datetime(1999, 1, 1).date(), now=NOW)
        assert payload["empty"] is True
        assert payload["arrivals"] == []
    finally:
        if enabled:
            with unit_of_work() as session:
                session.query(FederationSource).filter(
                    FederationSource.source_id.in_(enabled)
                ).update({FederationSource.enabled: True}, synchronize_session=False)


def test_feed_skips_noise_before_the_limit_and_clear_restores_the_row():
    base = datetime(2999, 1, 1)
    with unit_of_work() as session:
        session.add(_finding("ov-keep", base, description="kept"))
        session.add_all(
            [
                _finding(
                    f"ov-marked-{i:02d}",
                    base + timedelta(minutes=i + 1),
                    noise_marked_at=base,
                    noise_marked_by="user-ov",
                )
                for i in range(FEED_LIMIT)
            ]
        )
    hidden = overview_payload(day=base.date(), now=base)
    ids = [row["finding_id"] for row in hidden["feed"]]
    assert "ov-keep" in ids
    assert not any(item.startswith("ov-marked-") for item in ids)

    with unit_of_work() as session:
        row = session.get(Finding, "ov-marked-00")
        assert row is not None
        row.noise_marked_at = None
        row.noise_marked_by = None
    restored = overview_payload(day=base.date(), now=base)
    assert "ov-marked-00" in [row["finding_id"] for row in restored["feed"]]


def test_feed_source_link_and_case_follow_the_existing_resolvers():
    at = datetime(2999, 2, 1)
    with unit_of_work() as session:
        session.add_all(
            [
                _case("ov-c-link", "open", at),
                _finding(
                    "ov-http",
                    at,
                    evidence_links=[
                        {"ref": "note"},
                        {"ref": "https://console.example/a"},
                    ],
                ),
                _finding(
                    "ov-nolink",
                    at + timedelta(seconds=1),
                    evidence_links=[{"ref": "not a url"}],
                ),
            ]
        )
        session.flush()
        _link(session, "ov-c-link", "ov-http")
    payload = overview_payload(day=at.date(), now=at)
    by_id = {row["finding_id"]: row for row in payload["feed"]}
    assert by_id["ov-http"]["source_link"] == "https://console.example/a"
    assert by_id["ov-http"]["case_id"] == "ov-c-link"
    assert by_id["ov-nolink"]["source_link"] is None
    assert by_id["ov-nolink"]["case_id"] is None


def test_overview_route_marks_unmeasured_nodes(client):
    response = client.get("/api/overview")
    assert response.status_code == 200
    body = response.json()
    for state in ("ticket", "dropped", "paused", "stuck", "incidents"):
        node = next(item for item in body["outcomes"] if item["state"] == state)
        assert node["count"] is None
        assert node["unmeasured_text"] == "Not measured yet"
