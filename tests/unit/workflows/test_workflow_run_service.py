"""Unit tests for the workflow_runs persistence layer (#127).

These exercise ``WorkflowRunService`` against a real Postgres (the
service's session lives on ``get_db_manager()``) so we're covering
the SQL schema + the ORM model mapping in one pass. A DB must be
reachable for these tests to run — CI has one; local runs skip
cleanly if not.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from core.storage.connection import get_db_manager
from core.storage.models import WorkflowRun
from core.workflows.workflow_run_service import WorkflowRunService, generate_run_id


def _db_available() -> bool:
    try:
        from core.storage.connection import get_db_manager

        m = get_db_manager()
        if m._engine is None:
            m.initialize()
        with m.session_scope() as s:
            s.execute(__import__("sqlalchemy").text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = [
    # Categorize as service-dependent so the no-service unit job deselects
    # these (`-m "not external_service"`) and the DB-backed CI job selects
    # them (`-m external_service`).
    pytest.mark.external_service,
    # Still skip cleanly on a local run with no DB reachable.
    pytest.mark.skipif(
        not _db_available(), reason="Postgres not reachable; skipping DB-backed tests"
    ),
]


@pytest.fixture
def service():
    return WorkflowRunService()


@pytest.fixture
def clean_runs():
    """Delete all test runs before/after each test to keep DB tidy."""
    from core.storage.connection import get_db_manager
    from sqlalchemy import text

    def _clear():
        with get_db_manager().session_scope() as s:
            s.execute(
                text("DELETE FROM workflow_runs WHERE workflow_id LIKE 'test-wf-%'")
            )

    _clear()
    yield
    _clear()


class TestRunIdFormat:
    def test_generate_run_id_shape(self):
        rid = generate_run_id()
        assert rid.startswith("wfr-")
        # wfr-YYYYMMDD-<uuid8>  → total 21 chars
        assert len(rid) == 21


class TestBeginAndFinalize:
    def test_begin_creates_running_row(self, service, clean_runs):
        run_id = service.begin_run(
            workflow_id="test-wf-001",
            workflow_name="Test WF",
            workflow_source="file",
            trigger_context={"finding_id": "f-test-123"},
            triggered_by="pytest",
            skill_tools_available=["skill_x"],
        )
        assert run_id is not None
        row = service.get_run(run_id)
        assert row is not None
        assert row["status"] == "running"
        assert row["workflow_id"] == "test-wf-001"
        assert row["workflow_name"] == "Test WF"
        assert row["triggered_by"] == "pytest"
        assert row["trigger_context"] == {"finding_id": "f-test-123"}
        assert row["skill_tools_available"] == ["skill_x"]
        assert row["finished_at"] is None
        assert row["duration_ms"] is None

    def test_finalize_stamps_status_duration_result(self, service, clean_runs):
        run_id = service.begin_run(
            workflow_id="test-wf-002",
            workflow_name="Test WF",
        )
        ok = service.finalize_run(
            run_id,
            status="completed",
            result_summary="All good.",
        )
        assert ok is True
        row = service.get_run(run_id)
        assert row["status"] == "completed"
        assert row["finished_at"] is not None
        assert row["duration_ms"] is not None
        assert row["duration_ms"] >= 0
        assert row["result_summary"] == "All good."
        assert row["error"] is None

    def test_finalize_failure_records_error(self, service, clean_runs):
        run_id = service.begin_run(
            workflow_id="test-wf-003",
            workflow_name="Test WF",
        )
        service.finalize_run(
            run_id,
            status="failed",
            error="RuntimeError: boom",
        )
        row = service.get_run(run_id)
        assert row["status"] == "failed"
        assert "RuntimeError" in (row["error"] or "")

    def test_finalize_counts_outcome_by_run_kind_from_trigger_context(
        self, service, clean_runs, monkeypatch
    ):
        counter = MagicMock()
        monkeypatch.setattr(
            "core.workflows.workflow_run_service._runs_finished", counter
        )
        run_id = service.begin_run(
            workflow_id="test-wf-kind",
            workflow_name="Test WF",
            trigger_context={"run_kind": "investigate"},
        )
        assert service.finalize_run(run_id, status="cancelled") is True
        counter.add.assert_called_once_with(
            1, {"run_kind": "investigate", "status": "cancelled"}
        )

    def test_finalize_rejects_bad_status(self, service, clean_runs):
        run_id = service.begin_run(
            workflow_id="test-wf-004",
            workflow_name="Test WF",
        )
        assert service.finalize_run(run_id, status="running") is False
        # Row should still be in running state.
        row = service.get_run(run_id)
        assert row["status"] == "running"

    def test_finalize_truncates_huge_result_summary(self, service, clean_runs):
        run_id = service.begin_run(
            workflow_id="test-wf-005",
            workflow_name="Test WF",
        )
        huge = "x" * 100_000
        service.finalize_run(run_id, status="completed", result_summary=huge)
        row = service.get_run(run_id)
        assert len(row["result_summary"]) == 50_000


class TestListRuns:
    def test_list_respects_workflow_filter_and_order(self, service, clean_runs):
        # Seed 3 runs across two workflows.
        a1 = service.begin_run(workflow_id="test-wf-A", workflow_name="A")
        a2 = service.begin_run(workflow_id="test-wf-A", workflow_name="A")
        b1 = service.begin_run(workflow_id="test-wf-B", workflow_name="B")

        runs_a = service.list_runs(workflow_id="test-wf-A")
        ids_a = {r["run_id"] for r in runs_a}
        assert {a1, a2}.issubset(ids_a)
        assert b1 not in ids_a

        # Newest first
        runs_all = service.list_runs()
        assert len(runs_all) >= 3

    def test_list_excludes_result_summary(self, service, clean_runs):
        run_id = service.begin_run(
            workflow_id="test-wf-list",
            workflow_name="list",
        )
        service.finalize_run(run_id, status="completed", result_summary="RESULT-BODY")
        runs = service.list_runs(workflow_id="test-wf-list")
        assert runs
        # ``list_runs`` calls to_dict(include_result=False) — result_summary
        # should not be in the envelope so list responses stay small.
        assert "result_summary" not in runs[0]

    def test_list_respects_started_at_and_finished_at_bounds(self, service, clean_runs):
        early = service.begin_run(workflow_id="test-wf-window", workflow_name="W")
        mid = service.begin_run(workflow_id="test-wf-window", workflow_name="W")
        late = service.begin_run(workflow_id="test-wf-window", workflow_name="W")
        for run_id in (early, mid, late):
            service.finalize_run(run_id, status="completed")

        t0 = datetime(2026, 1, 1, 12, 0, 0)
        t1 = datetime(2026, 2, 1, 12, 0, 0)
        t2 = datetime(2026, 3, 1, 12, 0, 0)
        stamps = {early: t0, mid: t1, late: t2}
        with get_db_manager().session_scope() as session:
            for run_id, started in stamps.items():
                row = session.get(WorkflowRun, run_id)
                row.started_at = started
                row.finished_at = started + timedelta(hours=1)

        in_window = service.list_runs(
            workflow_id="test-wf-window",
            status="completed",
            started_at=datetime(2026, 1, 15),
            finished_at=datetime(2026, 2, 15),
        )
        ids = {row["run_id"] for row in in_window}
        assert ids == {mid}

    def test_list_finished_after_includes_runs_that_started_earlier(
        self, service, clean_runs
    ):
        overlap = service.begin_run(workflow_id="test-wf-finish", workflow_name="W")
        mid = service.begin_run(workflow_id="test-wf-finish", workflow_name="W")
        late = service.begin_run(workflow_id="test-wf-finish", workflow_name="W")
        for run_id in (overlap, mid, late):
            service.finalize_run(run_id, status="completed")

        t_start = datetime(2026, 1, 1, 12, 0, 0)
        t_mid = datetime(2026, 2, 1, 12, 0, 0)
        t_late = datetime(2026, 3, 1, 12, 0, 0)
        with get_db_manager().session_scope() as session:
            for run_id, started, finished in (
                (overlap, t_start, t_mid),
                (mid, t_mid, t_mid + timedelta(hours=1)),
                (late, t_late, t_late + timedelta(hours=1)),
            ):
                row = session.get(WorkflowRun, run_id)
                row.started_at = started
                row.finished_at = finished

        in_window = service.list_runs(
            workflow_id="test-wf-finish",
            status="completed",
            finished_after=datetime(2026, 1, 15),
            finished_at=datetime(2026, 2, 15),
        )
        ids = {row["run_id"] for row in in_window}
        assert ids == {overlap, mid}
