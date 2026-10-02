"""GET /cases reads needs_you once and marks rows from that same set."""

import asyncio
from contextlib import contextmanager
from datetime import datetime

import pytest

from core.api.v1 import cases_router
from core.storage.case_repository import CaseQueueRow, CaseQueueStrip

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 15, 12, 0, 0)


def _row(case_id: str) -> CaseQueueRow:
    return CaseQueueRow(
        case_id=case_id,
        title=case_id,
        priority="medium",
        assignee=None,
        status="open",
        live_status=None,
        workflow_id=None,
        findings_count=0,
        iteration_count=None,
        cost_usd=None,
        max_cost_usd=None,
        comment_count=0,
        last_activity=NOW,
        age_seconds=0,
        has_sla=False,
        sla_created_at=None,
        response_due=None,
        resolution_due=None,
        response_completed_at=None,
        resolution_completed_at=None,
        is_paused=False,
    )


def test_get_cases_passes_one_needs_you_read_into_the_page(monkeypatch):
    seen = {"calls": 0, "ids": None}

    def fake_needs_you(case_id=None):
        seen["calls"] += 1
        return {
            "count": 4,
            "items": [
                {"case_id": "waiting"},
                {"case_id": ""},
                {"case_id": None},
                {"case_id": "sooner"},
                "not-a-row",
            ],
        }

    class Repo:
        def __init__(self, session):
            pass

        def queue(self, **kwargs):
            seen["ids"] = kwargs["needs_you_ids"]
            seen["offset"] = kwargs["offset"]
            return [_row("waiting"), _row("closer")], 3

        def strip(self, now=None):
            return CaseQueueStrip(
                by_state={},
                sla_at_risk=0,
                closed_today=0,
                agent_closure_share=0.0,
            )

    @contextmanager
    def uow():
        yield object()

    monkeypatch.setattr(cases_router.data_service, "is_using_database", lambda: True)
    monkeypatch.setattr(cases_router, "needs_you", fake_needs_you)
    monkeypatch.setattr(cases_router, "CaseRepository", Repo)
    monkeypatch.setattr(cases_router, "unit_of_work", uow)

    body = asyncio.run(cases_router.get_cases(limit=1, offset=1))

    assert seen["calls"] == 1
    assert seen["ids"] == {"waiting", "sooner"}
    assert seen["offset"] == 1
    assert [row["case_id"] for row in body["cases"]] == ["waiting", "closer"]
    assert [row["needs_you"] for row in body["cases"]] == [True, False]
