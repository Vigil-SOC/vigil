"""Demo mode serves findings and cases on the screens that used to skip them."""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime

import pytest

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 27, 12, 0, 0)


@pytest.fixture
def demo_service():
    from core.platform.demo_data_service import DemoDataService

    saved_random = random.getstate()
    saved = (
        DemoDataService._instance,
        list(DemoDataService._findings),
        list(DemoDataService._cases),
        DemoDataService._initialized,
    )
    DemoDataService._instance = None
    DemoDataService._findings = []
    DemoDataService._cases = []
    DemoDataService._initialized = False
    service = DemoDataService()
    yield service
    (
        DemoDataService._instance,
        DemoDataService._findings,
        DemoDataService._cases,
        DemoDataService._initialized,
    ) = saved
    random.setstate(saved_random)


def _forbid_database(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("demo read opened a database")

    monkeypatch.setattr("core.storage.connection.get_db_manager", explode)
    monkeypatch.setattr("core.findings.overview.get_db_manager", explode)
    monkeypatch.setattr("services.api.triage_read.get_db_manager", explode)
    monkeypatch.setattr("core.response.approval_service.get_db_manager", explode)


def test_generated_set_matches_the_dashboard_counts(demo_service):
    findings = demo_service.get_findings()
    cases = demo_service.get_cases()
    assert len(findings) == 25
    assert len(cases) == 5
    assert Counter(case["status"] for case in cases) == {
        "open": 1,
        "investigating": 3,
        "closed": 1,
    }


def test_case_queue_lists_the_open_demo_cases_and_keeps_the_closed_one(demo_service):
    from core.findings.demo_reads import case_queue

    findings = demo_service.get_findings()
    cases = demo_service.get_cases()
    body = case_queue(cases, findings, now=NOW, limit=100, offset=0)

    assert body["total"] == 4
    assert body["has_more"] is False
    assert Counter(row["combined_state"] for row in body["cases"]) == {
        "investigating": 3,
        "open": 1,
    }
    assert all(row["needs_you"] for row in body["cases"])
    assert all(row["findings_count"] >= 2 for row in body["cases"])
    assert body["strip"]["by_state"] == {"investigating": 3, "open": 1}
    assert body["strip"]["needs_you"] == 4
    assert body["strip"]["sla_at_risk"] == 0

    closed = case_queue(cases, findings, now=NOW, limit=100, offset=0, closed=True)
    assert closed["total"] == 1
    assert closed["cases"][0]["combined_state"] == "closed"
    assert closed["cases"][0]["needs_you"] is False
    # The strip is the open queue, not the page.
    assert closed["strip"]["needs_you"] == 4


def test_case_queue_filters_without_dropping_the_strip(demo_service):
    from core.findings.demo_reads import case_queue

    findings = demo_service.get_findings()
    cases = demo_service.get_cases()
    case_id = next(case["case_id"] for case in cases if case["status"] != "closed")
    queried = case_queue(cases, findings, now=NOW, limit=10, offset=0, query=case_id)
    assert queried["total"] == 1
    assert queried["cases"][0]["case_id"] == case_id
    assert queried["strip"]["by_state"]["investigating"] == 3

    assert (
        case_queue(
            cases, findings, now=NOW, limit=10, offset=0, state="open"
        )["total"]
        == 1
    )
    assert (
        case_queue(
            cases, findings, now=NOW, limit=10, offset=0, sla_at_risk=True
        )["total"]
        == 0
    )
    assert (
        case_queue(
            cases, findings, now=NOW, limit=10, offset=0, workflow_constrained=True
        )["total"]
        == 0
    )
    open_case = next(case for case in cases if case["status"] != "closed")
    by_id = {finding["finding_id"]: finding for finding in findings}
    source = by_id[open_case["finding_ids"][0]]["data_source"]
    by_source = case_queue(
        cases, findings, now=NOW, limit=10, offset=0, data_source=source
    )
    assert by_source["total"] >= 1
    assert any(row["case_id"] == open_case["case_id"] for row in by_source["cases"])
    page = case_queue(cases, findings, now=NOW, limit=2, offset=2)
    assert len(page["cases"]) == 2
    assert page["total"] == 4
    assert page["has_more"] is False


def test_get_cases_uses_demo_rows_and_does_not_open_the_database(demo_service, monkeypatch):
    from core.api.v1 import cases_router

    _forbid_database(monkeypatch)
    monkeypatch.setattr(cases_router.data_service, "is_demo_mode", lambda: True)
    monkeypatch.setattr(cases_router, "unit_of_work", lambda: (_ for _ in ()).throw(AssertionError("uow")))

    body = cases_router.get_cases(
        limit=100,
        offset=0,
        needs_you_only=False,
        sla_at_risk=False,
        workflows=object(),
    )
    from core.storage.schemas.case_api import CaseListResponse

    CaseListResponse.model_validate(body)
    assert body["total"] == 4
    assert len(body["cases"]) == 4


def test_linked_findings_come_from_demo_rows(demo_service):
    from core.findings.demo_reads import linked_findings

    findings = demo_service.get_findings()
    case = demo_service.get_cases()[0]
    rows = linked_findings(case["finding_ids"], findings)
    assert [row["finding_id"] for row in rows] == case["finding_ids"]
    assert all(row["description"] for row in rows)
    assert linked_findings(["missing"], findings) == []


def test_overview_feed_is_the_demo_findings(demo_service, monkeypatch):
    from core.findings.overview import overview_alert, overview_payload

    _forbid_database(monkeypatch)
    monkeypatch.setattr("core.config.is_demo_mode", lambda: True)
    monkeypatch.setattr(
        "core.findings.overview._demo_agents",
        lambda: [
            {
                "workflow_id": "triage",
                "name": "Triage",
                "running": 0,
                "sample_size": 0,
                "rate": None,
                "level": None,
                "current_step": None,
            }
        ],
    )

    body = overview_payload(now=NOW)
    finding_ids = {row["finding_id"] for row in demo_service.get_findings()}
    assert body["empty"] is False
    assert {row["finding_id"] for row in body["feed"]} == finding_ids
    assert all(
        row["data_source"] in {f["data_source"] for f in demo_service.get_findings()}
        for row in body["feed"]
    )
    counted = sum(node["count"] for node in body["outcomes"] if node["count"] is not None)
    assert counted == sum(row["count"] for row in body["arrivals"])
    assert {row["data_source"] for row in body["arrivals"]} <= {
        finding["data_source"] for finding in demo_service.get_findings()
    }
    assert body["agents"][0]["running"] == 0
    sample = body["feed"][0]
    assert overview_alert(sample["finding_id"])["finding_id"] == sample["finding_id"]
    assert overview_alert("not-a-demo-finding") is None
    # A finding inside a case names that case; one outside does not.
    linked = {finding_id for case in demo_service.get_cases() for finding_id in case["finding_ids"]}
    cased = [row for row in body["feed"] if row["finding_id"] in linked]
    loose = [row for row in body["feed"] if row["finding_id"] not in linked]
    assert cased and loose
    assert all(row["case_id"] for row in cased)
    assert any(row["terminal_state"] == "needs_you" for row in cased)
    assert all(row["terminal_state"] in {"needs_you", "resolved_auto"} for row in cased)
    assert all(row["case_id"] is None and row["terminal_state"] == "waiting" for row in loose)


def test_triage_rows_are_demo_findings_not_stored_intake(demo_service, monkeypatch):
    from services.api.triage_read import triage_payload

    _forbid_database(monkeypatch)
    monkeypatch.setattr("core.config.is_demo_mode", lambda: True)

    body = triage_payload(now=NOW)
    findings = demo_service.get_findings()
    assert body["counts"]["total"] == 25
    assert body["matched"] == 25
    assert {row["finding_id"] for row in body["rows"]} == {row["finding_id"] for row in findings}
    assert body["counts"]["kind"] == {"detection": 25}
    launched = [row for row in body["rows"] if row["state"] == "launched"]
    waiting = [row for row in body["rows"] if row["state"] == "queued"]
    assert launched and waiting
    assert all(row["state_label"] == "Started a case" and row["case_door"] for row in launched)
    assert all(row["state_label"] == "Waiting for a slot" and row["case_door"] is None for row in waiting)
    source = body["rows"][0]["source"]
    narrowed = triage_payload(now=NOW, source=source)
    assert narrowed["matched"] == body["counts"]["source"][source]
    assert all(row["source"] == source for row in narrowed["rows"])
    assert {row["data_source"] for row in body["sources"]} <= set(body["counts"]["source"])
    assert body["strip"]["waiting"] == len(waiting)


def test_triage_dismissed_lists_the_demo_findings_marked_as_noise(
    demo_service, monkeypatch
):
    from services.api.triage_read import triage_payload

    _forbid_database(monkeypatch)
    monkeypatch.setattr("core.config.is_demo_mode", lambda: True)
    marked = demo_service.get_findings()[0]
    marked["noise_marked_at"] = "2026-10-27T00:00:00+00:00"

    body = triage_payload(now=NOW, state="dismissed")
    assert body["counts"]["state"]["dismissed"] == 1
    assert body["matched"] == 1
    assert [row["finding_id"] for row in body["rows"]] == [marked["finding_id"]]


def test_needs_you_lists_open_demo_cases(demo_service, monkeypatch):
    from core.response.approval_service import needs_you

    _forbid_database(monkeypatch)
    monkeypatch.setattr("core.config.is_demo_mode", lambda: True)

    body = needs_you()
    from core.api.v1.approvals_router import NeedsYouResponse

    NeedsYouResponse.model_validate(body)
    open_ids = {case["case_id"] for case in demo_service.get_cases() if case["status"] != "closed"}
    assert body["count"] == 4
    assert {item["case_id"] for item in body["items"]} == open_ids
    assert {item["kind"] for item in body["items"]} == {"open_case"}
    assert all(item["source_id"] == item["case_id"] for item in body["items"])
    assert all(item["created_at"].endswith("+00:00") for item in body["items"])

    one = next(iter(open_ids))
    only = needs_you(one)
    assert [item["case_id"] for item in only["items"]] == [one]


def test_a_dismissed_demo_finding_stays_in_todays_inflow(demo_service):
    from datetime import timezone

    from core.findings.demo_reads import overview_parts

    findings = demo_service.get_findings()
    cases = demo_service.get_cases()
    marked = dict(findings[0])
    marked["noise_marked_at"] = "2026-10-27T00:00:00+00:00"
    marked["timestamp"] = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc).isoformat()
    parts = overview_parts([marked], cases, day=datetime(2026, 10, 27).date(), feed_limit=50)
    assert parts["states"][0]["terminal_state"] == "dismissed"
    assert parts["arrivals"][0]["count"] == 1
    assert parts["feed"] == []


def test_case_detail_and_record_render_without_a_database(demo_service, monkeypatch):
    import asyncio

    from core.api.v1 import cases_router
    from core.storage.schemas.case_api import CaseDetailResponse
    from services.api.routers import cases as case_record

    monkeypatch.setattr(cases_router.data_service, "is_demo_mode", lambda: True)
    monkeypatch.setattr(case_record.data_service, "is_demo_mode", lambda: True)
    case = next(row for row in demo_service.get_cases() if row["status"] != "closed")

    detail = cases_router.get_case(case["case_id"], session=None)
    CaseDetailResponse.model_validate(detail)
    assert detail["case_id"] == case["case_id"]
    assert [row["finding_id"] for row in detail["linked_findings"]] == case["finding_ids"]
    assert detail["investigations"] == []

    record = asyncio.run(case_record.get_case_record(case["case_id"], session=None))
    assert record["run_id"] is None
    assert record["rows"] == []
