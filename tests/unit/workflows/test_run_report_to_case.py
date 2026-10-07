# Picking a case in the run modal was read-only: it pasted case detail into the
# prompt and nothing came back, so a hunt's report lived only in the run row.

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from core.workflows.run_bridge_router import (
    TerminalHandoff,
    TerminalUpdate,
    record_terminal,
)

pytestmark = pytest.mark.unit

REPORT = "# Hunt report\n\n1 hypothesis proven."


class _Cases:
    """The case table, as much of it as the bridge touches."""

    def __init__(self, cases: Dict[str, Dict[str, Any]]):
        self.cases = cases
        self.created: List[Dict[str, Any]] = []

    def get_case(self, case_id: str):
        return self.cases.get(case_id)

    def update_case(self, case_id: str, **updates):
        self.cases[case_id].update(updates)
        return True

    def create_case(self, **kwargs):
        opened = {"case_id": f"case-new-{len(self.created)}", **kwargs}
        self.created.append(opened)
        self.cases[opened["case_id"]] = opened
        return opened


class _Runs:
    def __init__(self, trigger_context: Dict[str, Any]):
        self.trigger_context = trigger_context
        self.finalized: Dict[str, Any] = {}

    def get_run(self, run_id: str):
        return {"run_id": run_id, "trigger_context": self.trigger_context}

    def finalize_run(self, run_id: str, **kwargs):
        self.finalized = {"run_id": run_id, **kwargs}


def _terminate(update: TerminalUpdate, trigger_context: Dict[str, Any], cases: _Cases):
    runs = _Runs(trigger_context)
    with patch(
        "core.storage.database_data_service.DatabaseDataService", return_value=cases
    ), patch("core.workflows.run_bridge_router.withdraw_for_run"), patch(
        "core.agents.internal_auth.authorise"
    ), patch(
        "core.workflows.run_bridge_router.authorise"
    ):
        record_terminal(
            run_id="run-1",
            update=update,
            authorization="Bearer x",
            run_service=runs,
            approvals=None,
        )
    return runs


def _activities(cases: _Cases, case_id: str) -> List[Dict[str, Any]]:
    return cases.cases[case_id].get("activities") or []


class TestTheReportReachesTheCase:
    def test_appends_the_report_to_the_case_the_run_was_started_from(self):
        cases = _Cases({"case-1": {"case_id": "case-1", "activities": []}})
        _terminate(
            TerminalUpdate(
                outcome="completed", reason="done", summary=REPORT, cost_usd=4.18
            ),
            {"case_id": "case-1"},
            cases,
        )

        [activity] = _activities(cases, "case-1")
        assert activity["activity_type"] == "agent_run_report"
        assert activity["description"] == REPORT
        assert activity["details"]["run_id"] == "run-1"
        assert activity["details"]["cost_usd"] == 4.18

    # Appended, never written over: the description is the analyst's own and a
    # case accumulates what was done to it.
    def test_keeps_what_the_case_already_recorded(self):
        cases = _Cases(
            {"case-1": {"case_id": "case-1", "activities": [{"activity_type": "note"}]}}
        )
        _terminate(
            TerminalUpdate(outcome="completed", summary=REPORT),
            {"case_id": "case-1"},
            cases,
        )

        assert [a["activity_type"] for a in _activities(cases, "case-1")] == [
            "note",
            "agent_run_report",
        ]

    def test_a_run_started_from_no_case_touches_none(self):
        cases = _Cases({"case-1": {"case_id": "case-1", "activities": []}})
        _terminate(
            TerminalUpdate(outcome="completed", summary=REPORT),
            {"finding_id": "f-1"},
            cases,
        )

        assert _activities(cases, "case-1") == []

    # The run ended either way. A case that was deleted mid-run must not turn a
    # finished run into a failed POST.
    def test_a_case_that_is_gone_does_not_fail_the_terminal(self):
        cases = _Cases({})
        runs = _terminate(
            TerminalUpdate(outcome="completed", summary=REPORT),
            {"case_id": "case-gone"},
            cases,
        )

        assert runs.finalized["status"] == "completed"

    def test_still_finalises_the_run_row(self):
        cases = _Cases({"case-1": {"case_id": "case-1", "activities": []}})
        runs = _terminate(
            TerminalUpdate(outcome="completed", summary=REPORT),
            {"case_id": "case-1"},
            cases,
        )

        assert runs.finalized["result_summary"] == REPORT
        assert runs.finalized["status"] == "completed"


class TestEscalationsLinkBothWays:
    HANDOFF = TerminalHandoff(
        case_id="case-25aac39c", title="beaconing host", markdown="# IR case"
    )

    def test_opens_its_own_case_so_the_escalation_stays_triageable(self):
        cases = _Cases({"case-1": {"case_id": "case-1", "activities": []}})
        _terminate(
            TerminalUpdate(
                outcome="completed", summary=REPORT, handoffs=[self.HANDOFF]
            ),
            {"case_id": "case-1"},
            cases,
        )

        assert [case["title"] for case in cases.created] == ["beaconing host"]

    def test_names_the_case_it_came_out_of(self):
        cases = _Cases({"case-1": {"case_id": "case-1", "activities": []}})
        _terminate(
            TerminalUpdate(
                outcome="completed", summary=REPORT, handoffs=[self.HANDOFF]
            ),
            {"case_id": "case-1"},
            cases,
        )

        assert "Escalated from case case-1" in cases.created[0]["description"]

    def test_and_the_case_it_came_out_of_names_it(self):
        cases = _Cases({"case-1": {"case_id": "case-1", "activities": []}})
        _terminate(
            TerminalUpdate(
                outcome="completed", summary=REPORT, handoffs=[self.HANDOFF]
            ),
            {"case_id": "case-1"},
            cases,
        )

        handoffs = [
            a
            for a in _activities(cases, "case-1")
            if a["activity_type"] == "agent_run_handoff"
        ]
        assert len(handoffs) == 1
        assert handoffs[0]["details"]["case_id"] == cases.created[0]["case_id"]

    # A hunt run from nowhere still escalates; there is simply nothing to link to.
    def test_an_escalation_with_no_origin_opens_a_case_and_links_nothing(self):
        cases = _Cases({})
        _terminate(
            TerminalUpdate(
                outcome="completed", summary=REPORT, handoffs=[self.HANDOFF]
            ),
            {},
            cases,
        )

        assert len(cases.created) == 1
        assert "Escalated from case" not in cases.created[0]["description"]
