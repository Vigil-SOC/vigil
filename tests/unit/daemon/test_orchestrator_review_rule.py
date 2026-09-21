"""The review decision records the completeness rule it turned on (#917).

``_review_investigation`` compares completeness to a literal 0.8; both the
approve and rework branches hand ``_log_ai_decision`` the rendered rule, and
``_log_ai_decision`` lands it in ``decision_metadata["rule"]``.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit


def _orchestrator(state: dict) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.workdir = MagicMock()
    orch.workdir.read_state.return_value = state
    orch.shared_intel = MagicMock()
    orch.stats = defaultdict(int)
    orch._update_investigation_status = MagicMock()
    orch._log_ai_decision = MagicMock()
    orch._send_notification = MagicMock()
    orch._create_approval_action = AsyncMock()
    orch._maybe_trigger_case_review = AsyncMock()
    return orch


@pytest.mark.asyncio
async def test_each_review_branch_records_the_completeness_rule():
    passed = _orchestrator(
        {"completed_steps": [1, 2, 3, 4, 5], "total_steps": 5, "summary": "done"}
    )
    await passed._review_investigation("inv-1")
    kwargs = passed._log_ai_decision.call_args.kwargs
    assert kwargs["decision_type"] == "review_approve"
    assert kwargs["rule"] == "review.completeness_floor=0.80 met (1.00)"

    rework = _orchestrator(
        {"completed_steps": [1, 2], "total_steps": 5, "summary": "partial"}
    )
    await rework._review_investigation("inv-2")
    kwargs = rework._log_ai_decision.call_args.kwargs
    assert kwargs["decision_type"] == "review_rework"
    assert kwargs["rule"] == "review.completeness_floor=0.80 not met (0.40)"

    # Complete but summaryless: the summary decided it, not the floor.
    no_summary = _orchestrator(
        {"completed_steps": [1, 2], "total_steps": 2, "summary": ""}
    )
    await no_summary._review_investigation("inv-3")
    kwargs = no_summary._log_ai_decision.call_args.kwargs
    assert kwargs["decision_type"] == "review_rework"
    assert kwargs["rule"] == "review.summary=missing"


def test_log_ai_decision_puts_the_rule_in_decision_metadata():
    orch = object.__new__(Orchestrator)
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = None

    @contextmanager
    def _scope():
        yield session

    manager = MagicMock()
    manager.session_scope = _scope
    with patch("core.storage.connection.get_db_manager", return_value=manager):
        orch._log_ai_decision(
            decision_type="review_approve",
            inv_id="inv-1",
            reasoning="ok",
            action="approve",
            confidence=1.0,
            rule="review.completeness_floor=0.80 met (1.00)",
        )
        orch._log_ai_decision(
            decision_type="plan", inv_id="inv-1", reasoning="ok", action="start"
        )

    with_rule, without_rule = (c.args[0] for c in session.add.call_args_list)
    assert with_rule.decision_metadata["rule"] == (
        "review.completeness_floor=0.80 met (1.00)"
    )
    assert "rule" not in without_rule.decision_metadata
