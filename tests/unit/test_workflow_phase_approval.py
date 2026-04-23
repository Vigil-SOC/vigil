"""Unit tests for phase-by-phase workflow execution + approval gating (#128).

These exercise the full pause→approve/reject→resume state machine
against a real Postgres. ``ClaudeService.chat`` is patched to return
canned per-phase text so we don't spend API credits and tests stay
deterministic.

Skips cleanly if no DB is reachable — same pattern as
``test_workflow_run_service.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List
from unittest.mock import patch

import pytest


def _db_available() -> bool:
    try:
        from database.connection import get_db_manager

        m = get_db_manager()
        if m._engine is None:
            m.initialize()
        with m.session_scope() as s:
            s.execute(__import__("sqlalchemy").text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="Postgres not reachable; skipping DB-backed tests"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_tables():
    """Wipe the tables we touch before + after each test."""
    from database.connection import get_db_manager
    from sqlalchemy import text

    def _clear():
        with get_db_manager().session_scope() as s:
            s.execute(
                text(
                    "DELETE FROM approval_actions WHERE workflow_run_id IN "
                    "(SELECT run_id FROM workflow_runs "
                    "WHERE workflow_id LIKE 'test-phase-%')"
                )
            )
            s.execute(
                text(
                    "DELETE FROM workflow_run_phases WHERE run_id IN "
                    "(SELECT run_id FROM workflow_runs "
                    "WHERE workflow_id LIKE 'test-phase-%')"
                )
            )
            s.execute(
                text("DELETE FROM workflow_runs WHERE workflow_id LIKE 'test-phase-%'")
            )

    _clear()
    yield
    _clear()


def _make_workflow(approval_on_phase_2: bool = True):
    """Build an in-memory WorkflowDefinition with 2 phases."""
    from services.workflows_service import WorkflowDefinition

    phases: List[Dict[str, Any]] = [
        {
            "order": 1,
            "phase_id": "phase-1",
            "name": "Triage",
            "agent_id": "triage",
            "purpose": "Initial triage",
            "tools": [],
            "steps": ["Check severity"],
            "expected_output": "triage summary",
            "approval_required": False,
        },
        {
            "order": 2,
            "phase_id": "phase-2",
            "name": "Respond",
            "agent_id": "auto_responder",
            "purpose": "Contain threat",
            "tools": [],
            "steps": ["Isolate host"],
            "expected_output": "response result",
            "approval_required": approval_on_phase_2,
        },
    ]
    metadata = {
        "name": "Test Phased Workflow",
        "description": "phase-by-phase execution test",
        "agents": ["triage", "auto_responder"],
        "tools-used": [],
        "use-case": "",
        "trigger-examples": [],
        "phases": phases,
    }
    return WorkflowDefinition(
        workflow_id="test-phase-001",
        file_path=None,
        metadata=metadata,
        body="",
        source="custom",
    )


class _FakeClaudeService:
    """Test double that skips the real Claude call but keeps the
    interface ``WorkflowsService`` depends on."""

    def __init__(self, *args, **kwargs):
        pass

    def has_api_key(self) -> bool:  # noqa: D401
        return True

    def chat(
        self, *, message, system_prompt, model, max_tokens, recommended_tools=None
    ):
        # Return a deterministic per-phase summary so we can assert on it.
        if "phase-1" in message or "Phase 1" in message:
            return "phase-1 output: triage complete"
        if "phase-2" in message or "Phase 2" in message:
            return "phase-2 output: contained"
        return "ok"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPhasedExecutionPauseResume:
    def _patched_service(self, workflow):
        """Return a WorkflowsService wired so .get_workflow yields our
        in-memory fixture and ClaudeService is the fake."""
        from services import workflows_service as ws

        svc = ws.WorkflowsService()
        svc.get_workflow = lambda wid: workflow  # type: ignore[method-assign]
        return svc

    def test_pauses_before_phase_requiring_approval(self, clean_tables):
        from services import claude_service as cs_module
        from services.workflow_run_service import get_workflow_run_service

        workflow = _make_workflow(approval_on_phase_2=True)
        svc = self._patched_service(workflow)

        with patch.object(cs_module, "ClaudeService", _FakeClaudeService):
            result = asyncio.run(
                svc.execute_workflow(
                    workflow.id,
                    parameters={"context": "test"},
                    triggered_by="pytest",
                )
            )

        assert result["success"] is True
        assert result["status"] == "paused"
        assert result["run_id"]
        assert result["pending_approval_action_id"]
        assert result["paused_at_phase"] == "phase-2"

        run = get_workflow_run_service().get_run(result["run_id"])
        assert run["status"] == "paused"
        phases = get_workflow_run_service().list_phases(result["run_id"])
        by_id = {p["phase_id"]: p for p in phases}
        assert by_id["phase-1"]["status"] == "completed"
        assert by_id["phase-2"]["status"] == "pending_approval"
        assert by_id["phase-2"]["approval_state"] == "pending"

    def test_resume_approved_completes_run(self, clean_tables):
        from services import claude_service as cs_module
        from services.approval_service import get_approval_service
        from services.workflow_run_service import get_workflow_run_service

        workflow = _make_workflow(approval_on_phase_2=True)
        svc = self._patched_service(workflow)

        with patch.object(cs_module, "ClaudeService", _FakeClaudeService):
            paused = asyncio.run(
                svc.execute_workflow(workflow.id, parameters={}, triggered_by="pytest")
            )
            assert paused["status"] == "paused"
            run_id = paused["run_id"]
            action_id = paused["pending_approval_action_id"]

            get_approval_service().approve_action(action_id, approved_by="tester")
            result = asyncio.run(
                svc.resume_workflow(run_id, "approved", approved_by="tester")
            )

        assert result["success"] is True
        assert result["status"] == "completed"
        assert result["run_id"] == run_id

        run = get_workflow_run_service().get_run(run_id)
        assert run["status"] == "completed"
        phases = get_workflow_run_service().list_phases(run_id)
        statuses = {p["phase_id"]: p["status"] for p in phases}
        approval_states = {p["phase_id"]: p["approval_state"] for p in phases}
        assert statuses == {"phase-1": "completed", "phase-2": "completed"}
        assert approval_states["phase-2"] == "approved"

    def test_resume_rejected_cancels_run(self, clean_tables):
        from services import claude_service as cs_module
        from services.approval_service import get_approval_service
        from services.workflow_run_service import get_workflow_run_service

        workflow = _make_workflow(approval_on_phase_2=True)
        svc = self._patched_service(workflow)

        with patch.object(cs_module, "ClaudeService", _FakeClaudeService):
            paused = asyncio.run(
                svc.execute_workflow(workflow.id, parameters={}, triggered_by="pytest")
            )
            run_id = paused["run_id"]
            action_id = paused["pending_approval_action_id"]

            get_approval_service().reject_action(
                action_id, reason="not safe", rejected_by="tester"
            )
            result = asyncio.run(
                svc.resume_workflow(
                    run_id,
                    "rejected",
                    rejection_reason="not safe",
                    approved_by="tester",
                )
            )

        assert result["success"] is True
        assert result["status"] == "cancelled"
        assert "not safe" in result["rejection_reason"]

        run = get_workflow_run_service().get_run(run_id)
        assert run["status"] == "cancelled"
        assert "not safe" in (run["error"] or "")
        phases = get_workflow_run_service().list_phases(run_id)
        by_id = {p["phase_id"]: p for p in phases}
        assert by_id["phase-2"]["status"] == "failed"
        assert by_id["phase-2"]["approval_state"] == "rejected"

    def test_no_approval_required_runs_straight_through(self, clean_tables):
        from services import claude_service as cs_module
        from services.workflow_run_service import get_workflow_run_service

        workflow = _make_workflow(approval_on_phase_2=False)
        svc = self._patched_service(workflow)

        with patch.object(cs_module, "ClaudeService", _FakeClaudeService):
            result = asyncio.run(
                svc.execute_workflow(workflow.id, parameters={}, triggered_by="pytest")
            )

        assert result["success"] is True
        assert result["status"] == "completed"
        run = get_workflow_run_service().get_run(result["run_id"])
        assert run["status"] == "completed"


class TestApprovalActionWorkflowLinkage:
    def test_create_action_persists_workflow_linkage(self, clean_tables):
        from services.approval_service import (
            ActionType,
            get_approval_service,
        )
        from services.workflow_run_service import get_workflow_run_service

        run_id = get_workflow_run_service().begin_run(
            workflow_id="test-phase-linkage",
            workflow_name="Linkage",
        )
        assert run_id is not None
        svc = get_approval_service()
        action = svc.create_action(
            action_type=ActionType.WORKFLOW_PHASE,
            title="phase approval",
            description="t",
            target=run_id,
            confidence=0.0,
            reason="approval_required",
            evidence=[run_id],
            created_by="pytest",
            workflow_run_id=run_id,
            workflow_phase_id="phase-2",
        )
        assert action.workflow_run_id == run_id
        assert action.workflow_phase_id == "phase-2"

        fetched = svc.get_action(action.action_id)
        assert fetched is not None
        assert fetched.workflow_run_id == run_id

        listed = svc.list_actions(workflow_run_id=run_id)
        assert any(a.action_id == action.action_id for a in listed)
