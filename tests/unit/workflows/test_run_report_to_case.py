# Picking a case in the run modal was read-only: it pasted case detail into the
# prompt and nothing came back, so a hunt's report lived only in the run row.

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from core.workflows import run_bridge_router as rbr
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

    def set_case(self, run_id: str, case_id: str):
        if self.trigger_context.get("case_id"):
            return False
        self.trigger_context = {**self.trigger_context, "case_id": case_id}
        return True


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
            registry=None,
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


class TestAHandoffLandsOnTheCaseTheRunCameFrom:
    HANDOFF = TerminalHandoff(
        case_id="case-25aac39c", title="beaconing host", markdown="# Handoff — hunt"
    )

    def _handoffs(self, cases: _Cases, case_id: str) -> List[Dict[str, Any]]:
        return [
            a
            for a in _activities(cases, case_id)
            if a["activity_type"] == "agent_run_handoff"
        ]

    def test_opens_no_second_case_and_records_one_handoff_on_the_origin(self):
        cases = _Cases({"case-1": {"case_id": "case-1", "activities": []}})
        _terminate(
            TerminalUpdate(
                outcome="completed", summary=REPORT, handoffs=[self.HANDOFF]
            ),
            {"case_id": "case-1"},
            cases,
        )

        assert cases.created == []
        [handoff] = self._handoffs(cases, "case-1")
        assert handoff["details"] == {
            "run_id": "run-1",
            "handoff_id": "case-25aac39c",
        }

    # Pushed the moment the hunt journals it, then again on the terminal.
    def test_a_second_arrival_records_no_second_handoff(self):
        cases = _Cases({"case-1": {"case_id": "case-1", "activities": []}})
        update = TerminalUpdate(
            outcome="completed", summary=REPORT, handoffs=[self.HANDOFF]
        )
        _terminate(update, {"case_id": "case-1"}, cases)
        _terminate(update, {"case_id": "case-1"}, cases)

        assert len(self._handoffs(cases, "case-1")) == 1

    def test_the_root_cause_run_is_filed_onto_the_origin_case(self):
        cases = _Cases({"case-1": {"case_id": "case-1", "activities": []}})
        with patch.object(rbr, "_source_is_hunt", return_value=True), patch.object(
            rbr, "_rca_exists", return_value=False
        ), patch.object(rbr, "_root_cause_unserved", return_value=[]), patch.object(
            rbr, "_start_root_cause"
        ) as start_rca:
            _terminate(
                TerminalUpdate(outcome="completed", handoffs=[self.HANDOFF]),
                {"case_id": "case-1"},
                cases,
            )

        assert start_rca.call_args.args[2] == "case-1"

    # No SIEM is the first-install case: the run would fail on its first turn.
    def test_with_telemetry_search_unbound_no_run_starts_and_the_case_says_why(self):
        cases = _Cases({"case-1": {"case_id": "case-1", "activities": []}})
        update = TerminalUpdate(outcome="completed", handoffs=[self.HANDOFF])
        with patch.object(rbr, "_source_is_hunt", return_value=True), patch.object(
            rbr, "_rca_exists", return_value=False
        ), patch.object(rbr, "_start_root_cause") as start_rca, patch.object(
            rbr, "_enqueue_root_cause"
        ) as enqueue:
            # registry=None binds no telemetry_search; the terminal re-carries the
            # handoff the /handoff push already filed.
            _terminate(update, {"case_id": "case-1"}, cases)
            _terminate(update, {"case_id": "case-1"}, cases)

        start_rca.assert_not_called()
        enqueue.assert_not_called()
        [said] = [
            a
            for a in _activities(cases, "case-1")
            if a["activity_type"] == "agent_run_root_cause_not_started"
        ]
        assert said["description"].startswith(
            "Root cause was not started: it needs a SIEM to search, and none is "
            "connected."
        )
        assert "telemetry_search" not in said["description"]
        assert said["details"] == {
            "run_id": "run-1",
            "handoff_id": "case-25aac39c",
            "unbound": ["telemetry_search"],
        }


class TestAHuntWithNoCaseOpensOne:
    HANDOFF = TerminalHandoff(
        case_id="case-25aac39c", title="beaconing host", markdown="# Handoff — hunt"
    )

    def test_names_the_run_that_handed_off_and_records_it(self):
        cases = _Cases({})
        _terminate(
            TerminalUpdate(
                outcome="completed", summary=REPORT, handoffs=[self.HANDOFF]
            ),
            {},
            cases,
        )

        [opened] = cases.created
        assert opened["title"] == "beaconing host"
        assert "Handed off by run run-1" in opened["description"]
        [activity] = _activities(cases, opened["case_id"])
        assert activity["activity_type"] == "agent_run_handoff"
        assert activity["details"]["run_id"] == "run-1"

    def test_a_repeat_delivery_opens_no_second_case(self):
        cases = _Cases({})
        update = TerminalUpdate(outcome="completed", handoffs=[self.HANDOFF])
        _terminate(update, {}, cases)
        # The real table loses the second insert on the derived primary key.
        cases.cases[rbr._handoff_case_id("run-1", self.HANDOFF)] = cases.created[0]
        _terminate(update, {}, cases)

        assert len(cases.created) == 1


class TestTheTitleIsCutOnAWholeWord:
    def test_leaves_a_short_title_alone(self):
        assert rbr._case_title("rare  beacon") == "rare beacon"

    def test_never_ends_mid_word(self):
        title = rbr._case_title("alpha beta gamma delta", limit=12)
        assert title == "alpha beta"

    def test_a_single_long_word_is_cut_at_the_limit(self):
        assert rbr._case_title("x" * 50, limit=20) == "x" * 20
