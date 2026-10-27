"""The Overview drill-down reads without a database (demo mode): totals match the board."""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime

import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit


@pytest.fixture
def demo(monkeypatch):
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

    def explode(*_args, **_kwargs):
        raise AssertionError("demo read opened a database")

    monkeypatch.setattr("core.findings.overview.get_db_manager", explode)
    monkeypatch.setattr("core.findings.overview_drill_router.get_db_manager", explode)
    monkeypatch.setattr("core.config.is_demo_mode", lambda: True)
    monkeypatch.setattr("core.findings.overview._demo_agents", lambda: [])
    yield DemoDataService()
    (
        DemoDataService._instance,
        DemoDataService._findings,
        DemoDataService._cases,
        DemoDataService._initialized,
    ) = saved
    random.setstate(saved_random)


def test_each_outcome_lists_as_many_alerts_as_the_board_counts(demo, monkeypatch):
    from core.findings.demo_reads import parse_utc
    from core.findings.overview import overview_payload
    from core.findings.overview_drill_router import outcome_alerts

    # The demo set spreads alerts over weeks: make "today" its busiest day.
    days = Counter(
        parse_utc(f.get("timestamp") or f.get("created_at")).date()
        for f in demo.get_findings()
    )
    day = days.most_common(1)[0][0]
    noon = datetime(day.year, day.month, day.day, 12)
    monkeypatch.setattr("core.findings.overview_drill_router.utcnow", lambda: noon)
    board = overview_payload(day=day, now=noon)
    measured = [node for node in board["outcomes"] if node["count"] is not None]
    assert sum(node["count"] for node in measured) > 0, "the demo set has alerts today"
    cases = {case["case_id"] for case in demo.get_cases()}
    for node in measured:
        body = outcome_alerts(node["state"], limit=5, offset=0)
        assert body["label"] == node["label"]
        assert body["total"] == node["count"]
        assert len(body["items"]) == min(5, node["count"])
        assert sum(s["count"] for s in body["by_source"]) == body["total"]
        times = [i["created_at"] for i in body["items"] if i["created_at"]]
        assert times == sorted(times, reverse=True)
        assert {c["case_id"] for c in body["cases"]} <= cases
        if node["state"] in ("dismissed", "scoring", "waiting"):
            assert body["cases"] == []
        else:
            assert sum(c["alerts"] for c in body["cases"]) == body["total"]
        rest = outcome_alerts(node["state"], limit=200, offset=5)
        assert len(rest["items"]) == max(0, node["count"] - 5)


def test_an_unknown_outcome_is_404(demo):
    from core.findings.overview_drill_router import outcome_alerts

    with pytest.raises(HTTPException) as err:
        outcome_alerts("nope", limit=5, offset=0)
    assert err.value.status_code == 404


def test_agent_record_in_demo_mode_reads_no_runs(demo, monkeypatch):
    from core.findings import overview_drill_router as drill

    monkeypatch.setattr(
        drill.catalog,
        "detail",
        lambda _service, wid: {"id": wid, "name": wid} if wid == "triage" else None,
    )
    body = drill.agent_detail("triage", service=None)
    assert body["workflow"]["id"] == "triage"
    assert body["live"] == [] and body["recent"] == []
    assert body["record"]["runs"] == 0 and body["record"]["rate"] is None
    with pytest.raises(HTTPException) as err:
        drill.agent_detail("no-such-workflow", service=None)
    assert err.value.status_code == 404


def test_record_counts_finished_runs():
    from core.findings.overview_drill_router import _record

    record = _record(
        [("completed", 1000, 0.5), ("completed", 3000, None), ("failed", 2000, 1.5)]
    )
    assert record["runs"] == 3
    assert record["completed"] == 2 and record["failed"] == 1
    assert record["rate"] == pytest.approx(2 / 3)
    assert record["median_ms"] == 2000
    assert record["mean_cost_usd"] == pytest.approx(1.0)
