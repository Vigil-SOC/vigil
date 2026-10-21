"""A concluded investigation that dismissed its case closes it (#2542).

benign and false_positive close the origin case as a false positive, by the agent,
in the run's name. Any other verdict, a run that did not complete, or a case closed
already leaves the case as it is.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from core.cases.closure import ClosedByKind, ClosureCategory
from core.workflows import run_bridge_router as rbr
from core.workflows.run_bridge_router import TerminalUpdate, record_terminal

pytestmark = pytest.mark.unit

CASE = "case-1"


class _Runs:
    def get_run(self, run_id: str) -> Dict[str, Any]:
        return {"run_id": run_id, "trigger_context": {"case_id": CASE}}

    def finalize_run(self, run_id: str, **kwargs: Any) -> bool:
        return True


class _Cases:
    def __init__(self) -> None:
        self.case: Dict[str, Any] = {
            "case_id": CASE,
            "priority": "low",
            "activities": [],
        }

    def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        return dict(self.case) if case_id == CASE else None

    def update_case(self, case_id: str, **updates: Any) -> bool:
        self.case.update(updates)
        return True


class _Workflow:
    """Stands in for CaseWorkflowService: the one closure writer."""

    closes: List[Dict[str, Any]] = []

    def close_case(self, session: Any, case_id: str, **kwargs: Any) -> Any:
        type(self).closes.append({"case_id": case_id, **kwargs})
        return MagicMock()


def _terminal(status: str = "open", **update: Any) -> _Cases:
    cases = _Cases()
    _Workflow.closes = []
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        case_id=CASE, status=status
    )

    @contextmanager
    def _session():
        yield session

    body = {
        "outcome": "completed",
        "reason": "done",
        "summary": "a scheduled task",
        **update,
    }
    with patch.object(rbr, "withdraw_for_run"), patch.object(
        rbr, "authorise"
    ), patch.object(rbr, "_source_is_hunt", return_value=False), patch(
        "core.storage.database_data_service.DatabaseDataService", return_value=cases
    ), patch(
        "core.cases.agent_closure.service_session", _session
    ), patch(
        "core.cases.case_workflow_service.CaseWorkflowService", _Workflow
    ):
        record_terminal("run-1", TerminalUpdate(**body), "Bearer x", _Runs(), None)
    return cases


def _closed(cases: _Cases) -> List[Dict[str, Any]]:
    return [
        a for a in cases.case["activities"] if a["activity_type"] == "agent_run_closed"
    ]


@pytest.mark.parametrize("disposition", ["benign", "false_positive", " Benign "])
def test_a_dismissal_closes_the_case_as_a_false_positive_by_the_agent(
    disposition: str,
) -> None:
    cases = _terminal(disposition=disposition)

    assert _Workflow.closes == [
        {
            "case_id": CASE,
            "closure_category": ClosureCategory.FALSE_POSITIVE,
            "closed_by": "run-1",
            "closed_by_kind": ClosedByKind.AGENT,
            "closure_notes": "a scheduled task",
        }
    ]
    [activity] = _closed(cases)
    assert activity["details"]["run_id"] == "run-1"


@pytest.mark.parametrize(
    "disposition", [None, "", "malicious", "suspicious", "needs_review", "inconclusive"]
)
def test_any_other_verdict_leaves_the_case_open(disposition: Optional[str]) -> None:
    cases = _terminal(disposition=disposition)

    assert _Workflow.closes == []
    assert _closed(cases) == []


@pytest.mark.parametrize(
    "outcome", ["failed", "budget_exhausted", "aborted", "abandoned"]
)
def test_a_run_that_did_not_complete_closes_nothing(outcome: str) -> None:
    cases = _terminal(outcome=outcome, disposition="benign")

    assert _Workflow.closes == []
    assert _closed(cases) == []


def test_a_case_closed_already_keeps_its_closure() -> None:
    # close_case would overwrite an analyst's false_positive close; it is never called.
    cases = _terminal(status="closed", disposition="benign")

    assert _Workflow.closes == []
    assert _closed(cases) == []


def test_a_reopened_case_is_closed_again() -> None:
    _terminal(status="open", disposition="false_positive")

    assert len(_Workflow.closes) == 1
