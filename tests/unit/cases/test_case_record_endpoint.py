"""GET /cases/{id}/record reads every run's ledger; an unreadable run is a row."""

from __future__ import annotations

from datetime import datetime

import pytest

from core.storage.models import WorkflowRun
from services.api.routers import cases

pytestmark = pytest.mark.unit


def _run(
    run_id: str, started: datetime, triggered_by: str = "", status: str = "completed"
) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        workflow_id="threat-hunt",
        status=status,
        started_at=started,
        triggered_by=triggered_by,
    )


def _ledger(at: str) -> list[dict]:
    return [{"seq": 1, "ts": at, "kind": "run", "payload": {}}]


@pytest.fixture
def record(monkeypatch):
    """The endpoint over these runs, with ``ledgers`` standing in for the agent."""

    def call(runs: list[WorkflowRun], ledgers: dict, activities=()):
        async def read_events(run_id):
            found = ledgers[run_id]
            if isinstance(found, Exception):
                raise found
            return found

        monkeypatch.setattr(cases, "read_events", read_events)
        monkeypatch.setattr(
            cases.data_service,
            "get_case",
            lambda _id: {"id": _id, "activities": list(activities)},
        )
        svc = cases.case_records_service
        monkeypatch.setattr(svc, "list_case_investigations", lambda *_: [])
        monkeypatch.setattr(svc, "list_case_runs", lambda *_: runs)
        monkeypatch.setattr(svc, "list_case_audit", lambda *_: [])
        import asyncio

        return asyncio.run(cases.get_case_record("case-1", session=None))

    return call


def test_every_run_is_read_and_the_primary_stays_the_case_run(record):
    hunt = _run("hunt", datetime(2026, 10, 14, 16, 0))
    root = _run("root", datetime(2026, 10, 14, 16, 5), "handoff:hunt:case-1")
    out = record(
        [root, hunt],
        {
            "hunt": _ledger("2026-10-14T16:00:01Z"),
            "root": _ledger("2026-10-14T16:05:01Z"),
        },
    )
    assert out["run_id"] == "hunt"
    assert [(row["id"], row["run_id"]) for row in out["rows"]] == [
        ("run:root:1", "root"),
        ("run:hunt:1", "hunt"),
    ]


def test_a_failed_or_missing_read_is_one_row_not_a_502(record):
    hunt = _run("hunt", datetime(2026, 10, 14, 16, 0))
    gone = _run("gone", datetime(2026, 10, 14, 16, 3))
    broken = _run("broken", datetime(2026, 10, 14, 16, 4))
    fresh = _run("fresh", datetime(2026, 10, 14, 16, 5), status="running")
    unknown = _run("unknown", datetime(2026, 10, 14, 16, 2))
    out = record(
        [fresh, broken, gone, unknown, hunt],
        {
            "hunt": _ledger("2026-10-14T16:00:01Z"),
            "gone": None,
            "broken": RuntimeError("the agent layer answered 500: boom"),
            "fresh": [],  # just started: no rows, and not unreadable
            "unknown": [],  # ended with nothing written: the agent never had it
        },
    )
    assert [(row["id"], row["text"]) for row in out["rows"]] == [
        (
            "run:broken:unread",
            "Could not read the record of run broken — the agent layer answered 500: boom",
        ),
        (
            "run:gone:unread",
            "Could not read the record of run gone — the agent layer has no such run",
        ),
        (
            "run:unknown:unread",
            "Could not read the record of run unknown — the agent layer has no events for it",
        ),
        ("run:hunt:1", "Run started"),
    ]


def test_a_case_with_no_runs_has_audit_and_activities_only(record):
    note = {"timestamp": "2026-10-14T16:00:00Z", "activity_type": "note"}
    out = record([], {}, [note])
    assert out["run_id"] is None and out["investigation_id"] is None
    assert [row["id"] for row in out["rows"]] == ["activity:0"]
