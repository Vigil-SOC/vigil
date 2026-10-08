"""Unit tests for phase approval gating (#128, rewired by #630).

The state machine is the same and it now spans the boundary: the agent
layer raises a checkpoint and reports the waiting step here, an analyst
answers on the approval action, and that answer is read back for the
agent layer to journal onto its ledger.

Skips cleanly if no DB is reachable — same pattern as
``test_workflow_run_service.py``.
"""

from __future__ import annotations

from typing import Optional

import pytest


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_tables():
    """Wipe the tables we touch before + after each test."""
    from sqlalchemy import text

    from core.storage.connection import get_db_manager

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


@pytest.fixture
def internal(monkeypatch):
    """Answer the shared-secret check so the bridge is callable directly.

    The token is the whole of it since ADR 0014 -- ``authorise`` no longer reads
    the request, so there is no loopback stand-in left to build.
    """
    from core.agents import internal_auth

    monkeypatch.setattr(internal_auth, "get_secret", lambda name: "test-token")
    return "Bearer test-token"


def _run(workflow_id: str = "test-phase-001") -> str:
    from core.workflows.workflow_run_service import WorkflowRunService

    run_id = WorkflowRunService().begin_run(
        workflow_id=workflow_id,
        workflow_name="Test Phased Workflow",
        workflow_source="file",
        triggered_by="tester",
    )
    assert run_id is not None
    return run_id


def _update(status: str, checkpoint: Optional[str] = None):
    from core.workflows.run_bridge_router import PhaseUpdate

    return PhaseUpdate(
        phase_id="phase-2",
        agent="auto_responder",
        name="Respond",
        order=2,
        status=status,
        checkpoint_id=checkpoint,
        question="Approve Respond, run by auto_responder?",
    )


def _services():
    from core.response.approval_service import ApprovalService
    from core.workflows.workflow_run_service import WorkflowRunService

    return WorkflowRunService(), ApprovalService()


class TestPhaseApprovalAcrossTheBridge:
    def test_a_waiting_phase_pauses_the_run_and_raises_an_approval(
        self, clean_tables, internal
    ):
        from core.response.approval_service import ActionStatus, ApprovalService
        from core.workflows.run_bridge_router import record_phase
        from core.workflows.workflow_run_service import WorkflowRunService

        token = internal
        run_id = _run()
        record_phase(
            run_id, _update("pending_approval", "chk-phase-2"), token, *_services()
        )

        run = WorkflowRunService().get_run(run_id)
        assert run["status"] == "paused"

        pending = ApprovalService().list_actions(
            status=ActionStatus.PENDING, workflow_run_id=run_id
        )
        assert len(pending) == 1
        assert pending[0].parameters["checkpoint_id"] == "chk-phase-2"
        assert pending[0].workflow_phase_id == "phase-2"

    def test_the_same_checkpoint_reported_twice_raises_one_approval(
        self, clean_tables, internal
    ):
        """A resume re-reports the step it is still waiting on."""
        from core.response.approval_service import ActionStatus, ApprovalService
        from core.workflows.run_bridge_router import record_phase

        token = internal
        run_id = _run()
        record_phase(
            run_id, _update("pending_approval", "chk-phase-2"), token, *_services()
        )
        record_phase(
            run_id, _update("pending_approval", "chk-phase-2"), token, *_services()
        )

        pending = ApprovalService().list_actions(
            status=ActionStatus.PENDING, workflow_run_id=run_id
        )
        assert len(pending) == 1

    def test_a_checkpoint_row_explains_itself_without_config(
        self, clean_tables, internal
    ):
        """A checkpoint compares no confidence, so no threshold belongs on it."""
        from core.response.approval_service import ActionStatus, ApprovalService
        from core.workflows.run_bridge_router import (
            CheckpointRaised,
            record_checkpoint,
        )

        run_id = _run()
        record_checkpoint(
            run_id,
            CheckpointRaised(
                checkpoint_id="cp-1",
                checkpoint_class="hypothesis_approval",
                question="Approve and start this hunt on 1 hypothesis",
            ),
            internal,
            ApprovalService(),
        )

        [row] = ApprovalService().list_actions(
            status=ActionStatus.PENDING, workflow_run_id=run_id
        )
        assert row.reason == "Vigil wants your go-ahead on the hypothesis it will test."
        assert "confidence_threshold" not in row.reason
        assert "hypothesis_approval" not in row.reason

    def test_an_approval_comes_back_as_a_decision_the_ledger_can_take(
        self, clean_tables, internal
    ):
        from core.response.approval_service import ActionStatus, ApprovalService
        from core.workflows.run_bridge_router import list_decisions, record_phase

        token = internal
        run_id = _run()
        record_phase(
            run_id, _update("pending_approval", "chk-phase-2"), token, *_services()
        )

        service = ApprovalService()
        action = service.list_actions(
            status=ActionStatus.PENDING, workflow_run_id=run_id
        )[0]
        service.approve_action(action.action_id, approved_by="tester")

        decisions = list_decisions(run_id, token, _services()[1]).decisions
        assert len(decisions) == 1
        assert decisions[0].checkpoint_id == "chk-phase-2"
        assert decisions[0].answer == "approve"
        assert decisions[0].actor == "tester"

    def test_a_rejection_comes_back_carrying_its_reason(self, clean_tables, internal):
        from core.response.approval_service import ActionStatus, ApprovalService
        from core.workflows.run_bridge_router import list_decisions, record_phase

        token = internal
        run_id = _run()
        record_phase(
            run_id, _update("pending_approval", "chk-phase-2"), token, *_services()
        )

        service = ApprovalService()
        action = service.list_actions(
            status=ActionStatus.PENDING, workflow_run_id=run_id
        )[0]
        service.reject_action(
            action.action_id, reason="too risky", rejected_by="tester"
        )

        decisions = list_decisions(run_id, token, _services()[1]).decisions
        assert len(decisions) == 1
        assert decisions[0].answer == "reject"
        assert decisions[0].text == "too risky"

    def test_an_undecided_run_hands_back_nothing_to_journal(
        self, clean_tables, internal
    ):
        from core.workflows.run_bridge_router import list_decisions, record_phase

        token = internal
        run_id = _run()
        record_phase(
            run_id, _update("pending_approval", "chk-phase-2"), token, *_services()
        )

        assert list_decisions(run_id, token, _services()[1]).decisions == []

    def test_a_completed_phase_leaves_the_run_running(self, clean_tables, internal):
        from core.workflows.run_bridge_router import record_phase
        from core.workflows.workflow_run_service import WorkflowRunService

        token = internal
        run_id = _run()
        record_phase(run_id, _update("completed"), token, *_services())

        run_service = WorkflowRunService()
        assert run_service.get_run(run_id)["status"] == "running"
        phases = run_service.list_phases(run_id)
        assert [p["phase_id"] for p in phases] == ["phase-2"]
        assert phases[0]["status"] == "completed"

    def test_the_outcome_finalises_the_run(self, clean_tables, internal):
        from core.workflows.run_bridge_router import TerminalUpdate, record_terminal
        from core.workflows.workflow_run_service import WorkflowRunService

        token = internal
        run_id = _run()
        record_terminal(
            run_id,
            TerminalUpdate(outcome="completed", reason="all 2 phases ran", summary="s"),
            token,
            *_services(),
        )

        run = WorkflowRunService().get_run(run_id)
        assert run["status"] == "completed"
        assert run["result_summary"] == "s"


class TestApprovalActionWorkflowLinkage:
    def test_create_action_persists_workflow_linkage(self, clean_tables):
        from core.response.approval_service import ActionType, ApprovalService
        from core.workflows.workflow_run_service import WorkflowRunService

        run_id = WorkflowRunService().begin_run(
            workflow_id="test-phase-linkage",
            workflow_name="Linkage",
        )
        assert run_id is not None
        svc = ApprovalService()
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
