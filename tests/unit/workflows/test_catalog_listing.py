"""Today's run count and mean cost on the workflow catalog."""

from datetime import datetime

import pytest

from core.storage.connection import get_db_manager
from core.storage.models import CustomWorkflow, WorkflowRun
from core.workflows.catalog import listing
from core.workflows.workflow_run_service import WorkflowRunService
from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.external_service

TODAY = datetime(2026, 6, 15, 9, 0, 0)
YESTERDAY = datetime(2026, 6, 14, 23, 0, 0)
UPDATED = datetime(2026, 6, 15, 8, 30, 0)


def _stamp(run_id: str, started: datetime, *, finished: bool) -> None:
    with get_db_manager().session_scope() as session:
        row = session.get(WorkflowRun, run_id)
        row.started_at = started
        row.finished_at = started if finished else None


def test_listing_attaches_todays_runs_and_mean_cost(monkeypatch):
    monkeypatch.setattr(
        "core.workflows.catalog.utcnow", lambda: datetime(2026, 6, 15, 16, 0, 0)
    )
    runs = WorkflowRunService()
    runs.begin_run(
        workflow_id="cloud-incident", workflow_name="cloud-incident", run_id="cat-a"
    )
    runs.finalize_run("cat-a", status="completed", cost_usd=1.5)
    runs.begin_run(
        workflow_id="cloud-incident", workflow_name="cloud-incident", run_id="cat-b"
    )
    runs.finalize_run("cat-b", status="completed", cost_usd=0.5)
    # Still running: counts, and its zero cost is not a finished mean.
    runs.begin_run(
        workflow_id="cloud-incident", workflow_name="cloud-incident", run_id="cat-open"
    )
    runs.begin_run(
        workflow_id="cloud-incident", workflow_name="cloud-incident", run_id="cat-old"
    )
    runs.finalize_run("cat-old", status="completed", cost_usd=9)
    runs.begin_run(
        workflow_id="cloud-incident", workflow_name="cloud-incident", run_id="cat-del"
    )
    runs.finalize_run("cat-del", status="completed", cost_usd=8)
    runs.delete_run("cat-del")
    # A finished run that cost nothing is a real zero.
    runs.begin_run(
        workflow_id="incident-response",
        workflow_name="incident-response",
        run_id="cat-zero",
    )
    runs.finalize_run("cat-zero", status="completed", cost_usd=0)

    for run_id in ("cat-a", "cat-b", "cat-open", "cat-del", "cat-zero"):
        _stamp(run_id, TODAY, finished=run_id != "cat-open")
    _stamp("cat-old", YESTERDAY, finished=True)

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
        assert "runs_today" in row
        assert "mean_cost_usd" in row

    cloud = by_id["cloud-incident"]
    assert cloud["runs_today"] == 3
    assert cloud["mean_cost_usd"] == pytest.approx(1.0)
    assert "updated_at" not in cloud

    zero = by_id["incident-response"]
    assert zero["runs_today"] == 1
    assert zero["mean_cost_usd"] == 0.0

    quiet = by_id["threat-hunt"]
    assert quiet["runs_today"] == 0
    assert quiet["mean_cost_usd"] is None

    custom = by_id["cat-custom"]
    assert custom["source"] == "custom"
    assert custom["updated_at"] == "2026-06-15T08:30:00+00:00"
    assert custom["runs_today"] == 0
    assert custom["mean_cost_usd"] is None
