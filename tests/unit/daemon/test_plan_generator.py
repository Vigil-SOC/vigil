"""Opening context must name the case the investigation already has (#867)."""

import pytest

from services.daemon.plan_generator import (
    WORKFLOW_STEP_MAP,
    generate_initial_context,
    generate_plan,
)


# The case is opened at admission (#920), so the plan must not send the agent
# looking for one or minting a second.
@pytest.mark.parametrize(
    "workflow_id", [w for w in WORKFLOW_STEP_MAP if w != "case-review"] + ["unknown"]
)
def test_case_management_step_attaches_to_the_admitted_case(workflow_id):
    plan = generate_plan("inv-1", workflow_id, [{"finding_id": "f-1"}], "c-42")
    assert "or create new case" not in plan
    assert "list_cases" not in plan
    assert "to the case named by case_id in this plan" in plan
    assert "create_case" not in plan
    assert "case_id: c-42" in plan


@pytest.mark.parametrize(
    "workflow_id", [w for w in WORKFLOW_STEP_MAP if w != "case-review"] + ["unknown"]
)
def test_case_management_step_creates_a_case_when_admission_left_none(workflow_id):
    plan = generate_plan("inv-1", workflow_id, [{"finding_id": "f-1"}])
    assert "case_id: pending" in plan
    assert "to the case named by case_id in this plan" not in plan
    assert "list_cases" not in plan
    assert "create one with create_case" in plan


def test_detection_plan_never_says_case_pending():
    plan = generate_plan(
        "inv-1", "incident-response", [{"finding_id": "f-1"}], "case-1"
    )
    assert "case_id: pending" not in plan
    assert "case_id: case-1" in plan


def test_includes_case_id_when_the_investigation_has_one():
    text = generate_initial_context(
        [{"finding_id": "f-1", "severity": "high", "description": "lockouts"}],
        case_id="c-42",
    )
    assert "case_id: c-42" in text
    assert "f-1" in text


def test_omits_case_id_when_the_investigation_has_none():
    text = generate_initial_context(
        [{"finding_id": "f-1", "severity": "high", "description": "lockouts"}]
    )
    assert "case_id:" not in text
