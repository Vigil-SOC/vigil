"""Review approves a review_submitted investigation on its terminal outcome (#1085)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

_REAL_STATE = {
    "investigation_id": "inv-1",
    "workflow_id": "incident-response",
    "case_id": "case-1",
    "status": "executing",
    "current_step": 1,
    "total_steps": 7,
    "trigger_finding_ids": [],
    "discovered_iocs": {},
    "discovered_entities": {},
    "proposed_actions": [],
    "blockers": [],
}


def _review(state: dict):
    orch = object.__new__(Orchestrator)
    orch.stats = {"investigations_completed": 0, "reviews_completed": 0}
    orch.shared_intel = MagicMock()
    orch.workdir = MagicMock()
    orch.workdir.read_state.return_value = state
    orch._update_investigation_status = MagicMock()
    orch._send_notification = MagicMock()
    orch._log_ai_decision = MagicMock()
    orch._maybe_trigger_case_review = AsyncMock()
    asyncio.run(orch._review_investigation("inv-1"))
    orch._update_investigation_status.assert_called_once_with("inv-1", "completed")
    kwargs = orch._log_ai_decision.call_args.kwargs
    return (
        kwargs["decision_type"],
        kwargs["rule"],
        kwargs["confidence"],
        orch._maybe_trigger_case_review,
    )


def test_review_submitted_approves_and_records_the_outcome():
    decision, rule, confidence, case_review = _review(
        {"proposed_actions": [], "workflow_id": "threat-hunt"}
    )
    assert (decision, rule, confidence) == (
        "review_approve",
        "review.terminal_outcome=completed",
        1.0,
    )
    case_review.assert_not_called()


def test_state_without_steps_or_summary_still_approves():
    assert "summary" not in _REAL_STATE
    assert "completed_steps" not in _REAL_STATE
    decision, rule, confidence, case_review = _review(_REAL_STATE)
    assert (decision, rule, confidence) == (
        "review_approve",
        "review.terminal_outcome=completed",
        1.0,
    )
    case_review.assert_awaited_once_with("case-1")


def test_case_review_workflow_does_not_trigger_another():
    state = {**_REAL_STATE, "workflow_id": "case-review"}
    _, _, _, case_review = _review(state)
    case_review.assert_not_called()
