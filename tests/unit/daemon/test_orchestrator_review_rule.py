"""A completed terminal outcome completes the investigation in one step (#1085, #2324)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

_STATE = {
    "investigation_id": "inv-1",
    "case_id": "case-1",
    "proposed_actions": [
        {"action": "block_ip", "target": "203.0.113.9", "requires_approval": True},
        {"action": "note", "requires_approval": False},
    ],
}


def _orch(status: dict) -> Orchestrator:
    """``status`` holds the row's status; the claim flips it like the DB would."""
    orch = object.__new__(Orchestrator)
    orch.stats = {"investigations_completed": 0}
    orch.shared_intel = MagicMock()
    orch.workdir = MagicMock()
    orch.workdir.read_state.return_value = _STATE
    orch._update_investigation_status = MagicMock()
    orch._record_progress = MagicMock()
    orch._send_notification = MagicMock()
    orch._log_ai_decision = MagicMock()
    orch._create_approval_action = AsyncMock()

    def claim(inv_id, notes):
        if status["value"] == "completed":
            return False
        status["value"] = "completed"
        return True

    orch._claim_completion = MagicMock(side_effect=claim)
    return orch


def _reconcile(orch, outcome: str = "completed"):
    projection = {"status": "terminal", "outcome": outcome, "reason": "done"}
    with patch(
        "services.daemon.orchestrator.read_projection",
        new=AsyncMock(return_value=projection),
    ):
        asyncio.run(orch._reconcile("inv-1"))


def test_completed_terminal_goes_straight_to_completed_with_its_side_effects():
    status = {"value": "executing"}
    orch = _orch(status)
    _reconcile(orch)

    assert status["value"] == "completed"
    orch._update_investigation_status.assert_not_called()
    assert orch.stats["investigations_completed"] == 1
    orch.shared_intel.close_investigation.assert_called_once_with("inv-1", "case-1")
    kwargs = orch._log_ai_decision.call_args.kwargs
    assert kwargs["rule"] == "review.terminal_outcome=completed"
    assert kwargs["confidence"] == 1.0
    orch._send_notification.assert_called_once()
    # Only the action that needs a person becomes an approval.
    orch._create_approval_action.assert_awaited_once_with(
        "inv-1", _STATE["proposed_actions"][0]
    )


def test_a_second_reconcile_of_the_same_run_repeats_nothing():
    status = {"value": "executing"}
    orch = _orch(status)
    _reconcile(orch)
    _reconcile(orch)

    assert orch.stats["investigations_completed"] == 1
    orch.shared_intel.close_investigation.assert_called_once()
    orch._log_ai_decision.assert_called_once()
    orch._send_notification.assert_called_once()
    orch._create_approval_action.assert_awaited_once()


def test_a_non_completed_outcome_still_fails():
    status = {"value": "executing"}
    orch = _orch(status)
    _reconcile(orch, "budget_exhausted")

    assert orch._update_investigation_status.call_args[0][1] == "failed"
    orch._claim_completion.assert_not_called()
    orch.shared_intel.close_investigation.assert_not_called()


def test_rows_left_in_review_submitted_are_completed_through_the_same_path():
    status = {"value": "review_submitted"}
    orch = _orch(status)
    orch._get_investigations_by_status = MagicMock(
        return_value=[{"investigation_id": "inv-1"}]
    )
    asyncio.run(orch._complete_legacy_review_rows())

    orch._get_investigations_by_status.assert_called_once_with("review_submitted")
    assert status["value"] == "completed"
    orch.shared_intel.close_investigation.assert_called_once_with("inv-1", "case-1")
    orch._create_approval_action.assert_awaited_once()
