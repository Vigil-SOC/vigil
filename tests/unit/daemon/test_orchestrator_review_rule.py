"""The review decision records the rule it fired on in decision_metadata (#917)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit


def _review(state: dict) -> tuple[str, str]:
    orch = object.__new__(Orchestrator)
    orch.stats = {"investigations_completed": 0, "reviews_completed": 0}
    orch.shared_intel = MagicMock()
    orch.workdir = MagicMock()
    orch.workdir.read_state.return_value = {"proposed_actions": [], **state}
    orch._update_investigation_status = MagicMock()
    orch._send_notification = MagicMock()
    orch._log_ai_decision = MagicMock()
    asyncio.run(orch._review_investigation("inv-1"))
    kwargs = orch._log_ai_decision.call_args.kwargs
    return kwargs["decision_type"], kwargs["rule"]


def test_approve_and_rework_record_the_completeness_floor():
    assert _review({"completed_steps": [1, 2, 3, 4], "total_steps": 5, "summary": "s"}) == (
        "review_approve",
        "review.completeness_floor=0.80 met (0.80)",
    )
    assert _review({"completed_steps": [1, 2, 3], "total_steps": 5, "summary": "s"}) == (
        "review_rework",
        "review.completeness_floor=0.80 not met (0.60)",
    )


def test_rework_on_missing_summary_does_not_blame_the_floor():
    assert _review({"completed_steps": [1, 2, 3, 4, 5], "total_steps": 5, "summary": ""}) == (
        "review_rework",
        "review.completeness_floor=0.80 met (1.00); review.summary=missing",
    )
