# An investigation that confirmed an intrusion ended with a rationale and nothing
# for anyone to approve, so the case's decisions stayed empty. What its conclusion
# proposes is filed as pending approvals on the case when the run ends.

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
import yaml

from core.response.approval_service import (
    ActionType,
    PendingAction,
    _case_id_for_action,
)
from core.workflows import run_bridge_router as rbr
from core.workflows.run_bridge_router import TerminalUpdate, record_terminal

pytestmark = pytest.mark.unit

PLUGIN = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "agent"
    / "plugins"
    / "investigate"
    / "plugin.yaml"
)

ISOLATE = {
    "action_type": "isolate_host",
    "target": "fin-ws-042",
    "reason": "beaconing to 45.77.53.176 every 300s",
    "confidence": 0.9,
}
BLOCK = {
    "action_type": "block_ip",
    "target": "45.77.53.176",
    "reason": "the beacon's destination",
}


class _Cases:
    def __init__(self, cases: Dict[str, Dict[str, Any]]):
        self.cases = cases

    def get_case(self, case_id: str):
        return self.cases.get(case_id)

    def update_case(self, case_id: str, **updates):
        self.cases[case_id].update(updates)
        return True


class _Runs:
    def __init__(self, trigger_context: Dict[str, Any]):
        self.trigger_context = trigger_context

    def get_run(self, run_id: str):
        return {"run_id": run_id, "trigger_context": self.trigger_context}

    def finalize_run(self, run_id: str, **kwargs):
        return True


class _Approvals:
    """The approval table, with create_action's idempotency and its
    human_only rule, and the two calls withdraw_for_run makes."""

    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []

    def create_action(self, **kwargs):
        key = kwargs.get("idempotency_key")
        for row in self.rows:
            if key and row["idempotency_key"] == key and row["status"] != "failed":
                return SimpleNamespace(**row)
        row = {
            **kwargs,
            "action_id": f"action-{len(self.rows)}",
            "action_type": kwargs["action_type"].value,
            "status": "pending" if kwargs.get("human_only") else "approved",
            "workflow_run_id": kwargs.get("workflow_run_id"),
            "idempotency_key": key,
        }
        self.rows.append(row)
        return SimpleNamespace(**row)

    def list_actions(self, status=None, workflow_run_id=None, **_):
        return [
            SimpleNamespace(**row)
            for row in self.rows
            if (status is None or row["status"] == status.value)
            and (workflow_run_id is None or row["workflow_run_id"] == workflow_run_id)
        ]

    def reject_action(self, action_id, reason, rejected_by="analyst"):
        for row in self.rows:
            if row["action_id"] == action_id:
                row["status"] = "rejected"

    def proposals(self) -> List[Dict[str, Any]]:
        return [r for r in self.rows if r.get("created_by") == "agent"]


def _terminate(
    update: TerminalUpdate,
    approvals: _Approvals,
    cases: _Cases,
    trigger_context: Optional[Dict[str, Any]] = None,
) -> None:
    runs = _Runs({"case_id": "case-1"} if trigger_context is None else trigger_context)
    with patch(
        "core.storage.database_data_service.DatabaseDataService", return_value=cases
    ), patch("core.workflows.run_bridge_router.authorise"):
        record_terminal(
            run_id="run-1",
            update=update,
            authorization="Bearer x",
            run_service=runs,
            approvals=approvals,
        )


def _case() -> _Cases:
    return _Cases({"case-1": {"case_id": "case-1", "activities": []}})


def _concluded(*proposals: Any) -> TerminalUpdate:
    return TerminalUpdate(
        outcome="completed",
        reason="beaconing confirmed",
        summary="beaconing confirmed",
        proposed_actions=list(proposals),
    )


class TestAConclusionFilesItsProposals:
    def test_each_one_waits_for_a_person_on_the_case(self):
        approvals = _Approvals()
        _terminate(_concluded(ISOLATE, BLOCK), approvals, _case())

        isolate, block = approvals.proposals()
        assert isolate["action_type"] == "isolate_host"
        assert isolate["target"] == "fin-ws-042"
        assert isolate["title"] == "Isolate host fin-ws-042"
        assert isolate["reason"] == ISOLATE["reason"]
        assert isolate["confidence"] == 0.9
        assert isolate["human_only"] is True
        assert isolate["status"] == "pending"
        assert isolate["parameters"] == {"case_id": "case-1", "run_id": "run-1"}
        # A confidence the model left out is no claim at all, not a high one.
        assert block["confidence"] == 0.0
        assert block["status"] == "pending"

    def test_each_one_resolves_to_the_case_for_its_decisions(self):
        approvals = _Approvals()
        _terminate(_concluded(ISOLATE), approvals, _case())

        [row] = approvals.proposals()
        action = PendingAction(
            action_id=row["action_id"],
            action_type=row["action_type"],
            title=row["title"],
            description=row["description"],
            target=row["target"],
            confidence=row["confidence"],
            reason=row["reason"],
            evidence=row["evidence"],
            created_at="",
            created_by=row["created_by"],
            requires_approval=True,
            status=row["status"],
            parameters=row["parameters"],
            workflow_run_id=row["workflow_run_id"],
        )
        assert _case_id_for_action(action, {}, {}) == "case-1"

    def test_the_case_says_how_many_were_proposed(self):
        cases = _case()
        _terminate(_concluded(ISOLATE, BLOCK), _Approvals(), cases)

        [report, proposed] = cases.cases["case-1"]["activities"]
        assert report["activity_type"] == "agent_run_report"
        assert proposed["activity_type"] == "actions_proposed"
        assert proposed["description"] == "Run run-1 proposed 2 actions for approval"
        assert proposed["details"] == {
            "run_id": "run-1",
            "action_ids": ["action-0", "action-1"],
        }


class TestTheRunEndingDoesNotTakeThemBack:
    def test_the_run_s_own_questions_are_withdrawn_and_the_proposals_are_not(self):
        approvals = _Approvals()
        # A checkpoint the run raised and nobody answered.
        approvals.rows.append(
            {
                "action_id": "checkpoint-0",
                "status": "pending",
                "workflow_run_id": "run-1",
                "idempotency_key": None,
            }
        )
        _terminate(_concluded(ISOLATE), approvals, _case())

        assert approvals.rows[0]["status"] == "rejected"
        [proposal] = approvals.proposals()
        assert proposal["status"] == "pending"
        assert proposal["workflow_run_id"] is None

    def test_a_terminal_that_arrives_twice_files_each_once(self):
        approvals, cases = _Approvals(), _case()
        _terminate(_concluded(ISOLATE, BLOCK), approvals, cases)
        _terminate(_concluded(ISOLATE, BLOCK), approvals, cases)

        assert [r["status"] for r in approvals.proposals()] == ["pending", "pending"]
        proposed = [
            a
            for a in cases.cases["case-1"]["activities"]
            if a["activity_type"] == "actions_proposed"
        ]
        assert len(proposed) == 1


class TestAMalformedProposalIsDropped:
    @pytest.mark.parametrize(
        "bad",
        [
            "isolate fin-ws-042",
            {**ISOLATE, "action_type": "wipe_disk"},
            {**ISOLATE, "action_type": "workflow_phase"},
            {**ISOLATE, "target": "  "},
            {**ISOLATE, "reason": ""},
            {k: v for k, v in ISOLATE.items() if k != "target"},
            {**ISOLATE, "confidence": 1.5},
            {**ISOLATE, "confidence": "high"},
            {**ISOLATE, "confidence": True},
        ],
    )
    def test_and_the_rest_are_still_filed(self, bad, caplog):
        approvals = _Approvals()
        with caplog.at_level("WARNING", logger=rbr.logger.name):
            _terminate(_concluded(bad, BLOCK), approvals, _case())

        assert [r["action_type"] for r in approvals.proposals()] == ["block_ip"]
        assert "dropped proposed action 0 of run-1" in caplog.text


class TestNothingIsProposedWhere:
    @pytest.mark.parametrize("outcome", ["budget_exhausted", "failed", "aborted"])
    def test_the_run_did_not_conclude(self, outcome):
        approvals = _Approvals()
        _terminate(
            TerminalUpdate(outcome=outcome, proposed_actions=[ISOLATE]),
            approvals,
            _case(),
        )

        assert approvals.proposals() == []

    def test_the_run_has_no_case(self):
        approvals = _Approvals()
        _terminate(_concluded(ISOLATE), approvals, _case(), trigger_context={})

        assert approvals.proposals() == []

    def test_the_conclusion_proposed_nothing(self):
        approvals, cases = _Approvals(), _case()
        _terminate(_concluded(), approvals, cases)

        assert approvals.proposals() == []
        assert [a["activity_type"] for a in cases.cases["case-1"]["activities"]] == [
            "agent_run_report"
        ]


# The lead is offered exactly the actions this side will file; one it could name
# and this side drops would be a proposal lost with only a log line to show for it.
def test_the_plugin_offers_the_actions_the_bridge_files():
    conclusion = yaml.safe_load(PLUGIN.read_text())["conclusion"]
    assert conclusion["proposals_field"] == "proposed_actions"
    item = conclusion["schema"]["properties"]["proposed_actions"]["items"]

    offered = item["properties"]["action_type"]["enum"]
    assert offered == list(rbr.PROPOSAL_TITLES)
    assert set(offered) <= {member.value for member in ActionType}
