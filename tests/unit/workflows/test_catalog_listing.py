"""The last 7 days of runs on the workflow catalog: count, success and cost."""

from datetime import datetime, timedelta

import pytest

from core.storage.connection import get_db_manager
from core.storage.models import CustomWorkflow, WorkflowRun
from core.workflows.catalog import listing
from core.workflows.workflow_run_service import WorkflowRunService
from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.external_service

NOW = datetime(2026, 6, 15, 16, 0, 0)
RECENT = NOW - timedelta(days=2)
UPDATED = datetime(2026, 6, 15, 8, 30, 0)


def _run(runs, run_id, workflow_id, started, status=None, cost=0.0):
    """One run begun at ``started``; ``status`` None leaves it running."""
    runs.begin_run(workflow_id=workflow_id, workflow_name=workflow_id, run_id=run_id)
    if status:
        runs.finalize_run(run_id, status=status, cost_usd=cost)
    with get_db_manager().session_scope() as session:
        row = session.get(WorkflowRun, run_id)
        row.started_at = started
        row.finished_at = started if status else None


def test_listing_attaches_seven_day_runs_success_and_mean_cost(monkeypatch):
    monkeypatch.setattr("core.workflows.catalog.utcnow", lambda: NOW)
    runs = WorkflowRunService()
    # cloud-incident: two statuses, one still running, one on the window edge,
    # one just outside it, one deleted.
    _run(runs, "cat-a", "cloud-incident", RECENT, "completed", 1.5)
    _run(runs, "cat-b", "cloud-incident", RECENT, "failed", 0.5)
    _run(runs, "cat-open", "cloud-incident", RECENT)
    _run(runs, "cat-edge", "cloud-incident", NOW - timedelta(days=7), "completed", 1.0)
    _run(
        runs,
        "cat-old",
        "cloud-incident",
        NOW - timedelta(days=7, seconds=1),
        "failed",
        9,
    )
    _run(runs, "cat-del", "cloud-incident", RECENT, "failed", 8)
    runs.delete_run("cat-del")
    # Only running runs: counted, but nothing has ended, so no rate or level.
    _run(runs, "cat-run1", "threat-hunt", RECENT)
    # A finished run that cost nothing is a real zero; cancelled is not a success.
    _run(runs, "cat-zero", "incident-response", RECENT, "completed", 0)
    _run(runs, "cat-cancel", "incident-response", RECENT, "cancelled", 0)

    with get_db_manager().session_scope() as session:
        session.add(
            CustomWorkflow(
                workflow_id="cat-custom",
                name="Catalog Custom",
                description="custom row",
                trigger_examples=[],
                phases=[],
                created_at=UPDATED,
                updated_at=UPDATED,
            )
        )

    try:
        by_id = {row["id"]: row for row in listing(WorkflowsService())["workflows"]}
    finally:
        with get_db_manager().session_scope() as session:
            session.query(WorkflowRun).filter(WorkflowRun.run_id.like("cat-%")).delete(
                synchronize_session=False
            )
            session.query(CustomWorkflow).filter(
                CustomWorkflow.workflow_id == "cat-custom"
            ).delete(synchronize_session=False)

    for row in by_id.values():
        assert "runs_today" not in row
        for key in ("runs_7d", "success_rate", "success_level", "mean_cost_usd"):
            assert key in row

    cloud = by_id["cloud-incident"]
    assert cloud["runs_7d"] == 4  # a, b, open, edge
    assert cloud["success_rate"] == pytest.approx(
        2 / 3
    )  # the running one has not ended
    assert cloud["success_level"] == "poor"
    assert cloud["mean_cost_usd"] == pytest.approx(1.0)  # a, b, edge; not open
    assert "updated_at" not in cloud

    mixed = by_id["incident-response"]
    assert mixed["runs_7d"] == 2
    assert mixed["success_rate"] == 0.5
    assert mixed["success_level"] == "poor"
    assert mixed["mean_cost_usd"] == 0.0

    running = by_id["threat-hunt"]
    assert running["runs_7d"] == 1
    assert running["success_rate"] is None
    assert running["success_level"] is None
    assert running["mean_cost_usd"] is None

    custom = by_id["cat-custom"]
    assert custom["source"] == "custom"
    assert custom["updated_at"] == "2026-06-15T08:30:00+00:00"
    assert custom["runs_7d"] == 0
    assert custom["success_rate"] is None
    assert custom["success_level"] is None
    assert custom["mean_cost_usd"] is None
