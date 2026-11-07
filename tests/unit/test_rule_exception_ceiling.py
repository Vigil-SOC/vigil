"""An agent's rule exception is held to the autonomy ceiling (#3949, #3950).

A run no person started may add or remove a rule exception, or save a rule with
exceptions, only when it may take a reversible action: Assist refuses it, Act
(the fleet's or the run's workflow tier) allows it, Stop and the environment
lock refuse it. A person-started run is that person acting: only their role is
checked. Other own writes (noise marks, case updates) are unchanged.
"""

from typing import Optional

import pytest

from core.agents import tools_router
from core.auth.tool_principal import Claims
from core.response import ceiling
from core.response.ceiling import STOPPED, Ceiling

HELD = "the autonomy ceiling does not let a run no person started make it"
ADD = {"action": "add", "rule_id": 7, "match": {"host.name": "backup-1"}, "note": "n"}
REMOVE = {"action": "remove", "exception_id": 3, "reason": "r"}
SAVE_WITH = {
    "splunk_yaml": "name: x\nsearch: index=a\n",
    "exceptions": {"splunk": [{"match": {"host": "backup-1"}, "note": "n"}]},
}
SAVE_EMPTY = {"splunk_yaml": "name: x\nsearch: index=a\n", "exceptions": {"splunk": []}}
SAVE_PLAIN = {"splunk_yaml": "name: x\nsearch: index=a\n"}

ACT_RUN, ASSIST_RUN = "run-in-act-workflow", "run-in-assist-workflow"


@pytest.fixture
def stored(monkeypatch):
    """Set the stored ceiling the check reads; own tools only, no lock."""
    box = {"ceiling": Ceiling()}
    monkeypatch.setattr(ceiling, "tool_classes", lambda names: {})
    monkeypatch.setattr(
        ceiling, "try_read_stored_ceiling", lambda: (box["ceiling"], True)
    )
    monkeypatch.setattr(
        ceiling,
        "workflow_of",
        lambda run_id: {ACT_RUN: "wf-act", ASSIST_RUN: "wf-assist"}.get(run_id),
    )
    monkeypatch.setattr(ceiling, "environment_locks_act", lambda settings=None: False)

    def put(**kw):
        box["ceiling"] = Ceiling(**kw)

    return put


def headless(tool, args, run_id: Optional[str] = "run-1"):
    return tools_router._denial(
        Claims(
            username=None,
            person_id=None,
            agent_id="triage",
            run_id=run_id,
            origin="schedule",
        ),
        tool,
        args,
    )


def person(tool, args):
    return tools_router._denial(
        Claims(
            username="analyst",
            person_id="user-1",
            agent_id="triage",
            run_id="run-p",
            origin="person",
        ),
        tool,
        args,
    )


# Assist (the default): refused, and nothing else changes.


@pytest.mark.parametrize("args", [ADD, REMOVE], ids=["add", "remove"])
def test_assist_refuses_a_headless_exception_change(stored, args):
    stored()
    refusal = headless("detection_rule_exception", args)
    assert refusal and HELD in refusal
    assert "changes what a rule detects" in refusal


def test_assist_refuses_a_headless_save_with_exceptions(stored):
    stored()
    refusal = headless("save_detection_rule", SAVE_WITH)
    assert refusal and HELD in refusal
    assert refusal.endswith("save it without exceptions")


@pytest.mark.parametrize("args", [SAVE_PLAIN, SAVE_EMPTY], ids=["none", "empty"])
def test_assist_lets_a_headless_save_without_exceptions_through(stored, args):
    stored()
    assert headless("save_detection_rule", args) is None


@pytest.mark.parametrize(
    "tool,args",
    [
        ("dismiss_finding", {"finding_id": "f-1", "reason": "noise"}),
        ("update_case", {"case_id": "c-1", "status": "closed"}),
    ],
    ids=["noise-mark", "case-close"],
)
def test_assist_leaves_other_own_writes_alone(stored, tool, args):
    stored()
    assert headless(tool, args) is None


# Act: the fleet's act_reversible, or the run's workflow tier.


@pytest.mark.parametrize(
    "tool,args",
    [
        ("detection_rule_exception", ADD),
        ("detection_rule_exception", REMOVE),
        ("save_detection_rule", SAVE_WITH),
    ],
    ids=["add", "remove", "save-with"],
)
def test_fleet_act_lets_a_headless_exception_change_through(stored, tool, args):
    stored(act_reversible=True)
    assert headless(tool, args) is None


def test_a_workflow_on_act_lets_its_runs_through_under_a_fleet_on_assist(stored):
    stored(workflows={"wf-act": {"tier": "act"}, "wf-assist": {"tier": "assist"}})
    assert headless("detection_rule_exception", ADD, run_id=ACT_RUN) is None
    assert headless("save_detection_rule", SAVE_WITH, run_id=ACT_RUN) is None
    assert HELD in headless("detection_rule_exception", ADD, run_id=ASSIST_RUN)


def test_a_workflow_on_assist_holds_its_runs_under_a_fleet_on_act(stored):
    stored(act_reversible=True, workflows={"wf-assist": {"tier": "assist"}})
    assert HELD in headless("detection_rule_exception", ADD, run_id=ASSIST_RUN)
    assert headless("detection_rule_exception", ADD, run_id="run-1") is None


# Stop and the environment lock beat Act.


@pytest.mark.parametrize(
    "tool,args",
    [
        ("detection_rule_exception", ADD),
        ("detection_rule_exception", REMOVE),
        ("save_detection_rule", SAVE_WITH),
        ("save_detection_rule", SAVE_PLAIN),
    ],
    ids=["add", "remove", "save-with", "save-plain"],
)
def test_stop_refuses_headless_writes_even_on_act(stored, tool, args):
    stored(act_reversible=True, stopped=True)
    assert headless(tool, args) == STOPPED


def test_the_environment_lock_holds_an_exception_change_on_act(stored, monkeypatch):
    stored(act_reversible=True)
    monkeypatch.setattr(ceiling, "environment_locks_act", lambda settings=None: True)
    assert HELD in headless("detection_rule_exception", ADD)
    assert headless("save_detection_rule", SAVE_PLAIN) is None


def test_an_unreadable_ceiling_grants_nothing(stored, monkeypatch):
    monkeypatch.setattr(ceiling, "try_read_stored_ceiling", lambda: None)
    assert "could not be read" in headless("detection_rule_exception", ADD)


# A person-started run is that person acting: only their role is checked.


@pytest.mark.parametrize(
    "ceiling_kw",
    [{}, {"stopped": True}],
    ids=["assist", "stopped"],
)
@pytest.mark.parametrize(
    "tool,args",
    [
        ("detection_rule_exception", ADD),
        ("detection_rule_exception", REMOVE),
        ("save_detection_rule", SAVE_WITH),
    ],
    ids=["add", "remove", "save-with"],
)
def test_a_person_started_run_is_held_to_the_role_not_the_ceiling(
    stored, monkeypatch, ceiling_kw, tool, args
):
    stored(**ceiling_kw)
    asked = []
    monkeypatch.setattr(
        tools_router, "permission_refusal", lambda p: asked.append(p) or None
    )
    assert person(tool, args) is None
    assert asked == ["detections.write"]


def test_a_person_without_the_role_is_refused_by_the_role(stored, monkeypatch):
    stored(act_reversible=True)
    monkeypatch.setattr(
        tools_router,
        "permission_refusal",
        lambda p: {"error": f"needs {p}"},
    )
    assert person("detection_rule_exception", ADD) == "needs detections.write"
