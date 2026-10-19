"""A concluded investigation raises its case's priority to the severity it found.

Only ever upward, and only from a run that completed with a rating Vigil knows.
Everything else leaves the priority the case already had.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from core.workflows import run_bridge_router as rbr
from core.workflows.run_bridge_router import TerminalUpdate, record_terminal

pytestmark = pytest.mark.unit

CASE = "case-1"


class _Runs:
    def __init__(self, case_id: Optional[str]) -> None:
        self.case_id = case_id

    def get_run(self, run_id: str) -> Dict[str, Any]:
        return {"run_id": run_id, "trigger_context": {"case_id": self.case_id}}

    def finalize_run(self, run_id: str, **kwargs: Any) -> bool:
        return True


class _Cases:
    def __init__(self, priority: Optional[str]) -> None:
        self.case: Dict[str, Any] = {
            "case_id": CASE,
            "priority": priority,
            "activities": [],
        }

    def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        return dict(self.case) if case_id == CASE else None

    def update_case(self, case_id: str, **updates: Any) -> bool:
        self.case.update(updates)
        return True


def _terminal(
    priority: Optional[str], origin: Optional[str] = CASE, **update: Any
) -> _Cases:
    cases = _Cases(priority)
    body = {"outcome": "completed", "reason": "done", "summary": "s", **update}
    with patch.object(rbr, "withdraw_for_run"), patch.object(
        rbr, "authorise"
    ), patch.object(rbr, "_source_is_hunt", return_value=False), patch(
        "core.storage.database_data_service.DatabaseDataService", return_value=cases
    ):
        record_terminal(
            "run-1", TerminalUpdate(**body), "Bearer x", _Runs(origin), None
        )
    return cases


def _raised(cases: _Cases) -> List[Dict[str, Any]]:
    return [
        a
        for a in cases.case["activities"]
        if a["activity_type"] == "agent_run_priority"
    ]


def test_a_higher_severity_raises_the_priority_and_says_so() -> None:
    cases = _terminal("low", severity="critical")

    assert cases.case["priority"] == "critical"
    [activity] = _raised(cases)
    assert activity["details"] == {"run_id": "run-1", "from": "low", "to": "critical"}


@pytest.mark.parametrize("priority", ["unknown", "", None])
def test_an_unrated_case_takes_the_severity(priority: Optional[str]) -> None:
    cases = _terminal(priority, severity="low")

    assert cases.case["priority"] == "low"
    [activity] = _raised(cases)
    assert activity["details"]["from"] == "unknown"


@pytest.mark.parametrize("severity", ["high", "low", "HIGH"])
def test_a_severity_at_or_below_the_priority_never_lowers_it(severity: str) -> None:
    cases = _terminal("high", severity=severity)

    assert cases.case["priority"] == "high"
    assert _raised(cases) == []


@pytest.mark.parametrize("severity", [None, "", "unknown", "info", "catastrophic"])
def test_a_missing_or_unrecognised_severity_is_ignored(severity: Optional[str]) -> None:
    cases = _terminal("low", severity=severity)

    assert cases.case["priority"] == "low"
    assert _raised(cases) == []


@pytest.mark.parametrize(
    "outcome", ["failed", "budget_exhausted", "aborted", "abandoned"]
)
def test_a_run_that_did_not_complete_leaves_the_priority(outcome: str) -> None:
    cases = _terminal("low", outcome=outcome, severity="critical")

    assert cases.case["priority"] == "low"
    assert _raised(cases) == []


def test_a_run_with_no_case_touches_none() -> None:
    cases = _terminal("low", origin=None, severity="critical")

    assert cases.case["priority"] == "low"
    assert cases.case["activities"] == []


def test_the_report_is_still_filed_beside_the_raise() -> None:
    cases = _terminal("medium", severity="high")

    kinds = [a["activity_type"] for a in cases.case["activities"]]
    assert kinds == ["agent_run_report", "agent_run_priority"]
