"""The bridge keeps the three-value status and stores the agent outcome beside it.

A budget stop is completed, and both an abort and an abandon are cancelled.
The list tells them apart from the outcome and reason written on the same row.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import patch

import pytest

from core.workflows.run_bridge_router import TerminalUpdate, record_terminal

pytestmark = pytest.mark.unit

# Folded status, then the reason the agent layer gave.
CASES = [
    ("completed", "completed", "the hunt wrote its report"),
    ("budget_exhausted", "completed", "hit the cost ceiling"),
    ("aborted", "cancelled", "the operator stopped it"),
    ("abandoned", "cancelled", "parked with no answer"),
    ("failed", "failed", "the worker crashed"),
]


class _Runs:
    def __init__(self) -> None:
        self.finalized: Dict[str, Any] = {}

    def get_run(self, run_id: str) -> Dict[str, Any]:
        return {"run_id": run_id, "trigger_context": {}}

    def finalize_run(self, run_id: str, **kwargs: Any) -> bool:
        self.finalized = kwargs
        return True


@pytest.mark.parametrize("outcome,status,reason", CASES)
def test_each_outcome_is_stored_beside_the_folded_status(
    outcome: str, status: str, reason: str
) -> None:
    runs = _Runs()
    with patch("core.workflows.run_bridge_router.withdraw_for_run"), patch(
        "core.workflows.run_bridge_router.authorise"
    ):
        record_terminal(
            "run-1",
            TerminalUpdate(outcome=outcome, reason=reason, summary="s"),
            "Bearer x",
            runs,
            None,
        )

    assert runs.finalized["status"] == status
    assert runs.finalized["outcome"] == outcome
    assert runs.finalized["reason"] == reason
    # The error column is the crash the history row renders in red.
    if status == "failed":
        assert runs.finalized["error"] == reason
    else:
        assert runs.finalized["error"] is None
