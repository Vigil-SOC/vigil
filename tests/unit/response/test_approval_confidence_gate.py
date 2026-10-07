"""A confidence a caller names cannot release an action it should not."""

import pytest

from core.response.approval_service import Reversibility
from core.response.autonomous_response_service import AutonomousResponseService
from core.response.config import (
    ResponseConfig,
    approval_requirement,
    response_action_decision,
)

pytestmark = pytest.mark.unit

REVERSIBLE = Reversibility.REVERSIBLE


@pytest.mark.parametrize("confidence", [1.01, 5.0, 42.0, -0.5, float("nan")])
def test_a_confidence_outside_zero_to_one_holds_the_action(confidence):
    required, rule = approval_requirement(
        False, REVERSIBLE, confidence, ResponseConfig()
    )
    assert required is True
    assert rule.startswith("response.confidence_range")


@pytest.mark.parametrize("confidence", [1.01, 42.0, float("nan")])
def test_a_confidence_outside_zero_to_one_decides_no_response(confidence):
    assert (
        response_action_decision("critical", confidence, "isolate", ResponseConfig())
        is None
    )


def test_an_ordinary_confidence_still_decides_by_the_threshold():
    config = ResponseConfig()
    assert approval_requirement(False, REVERSIBLE, 0.95, config)[0] is False
    assert approval_requirement(False, REVERSIBLE, 0.50, config)[0] is True


class _Service:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def list_actions(self, status=None, **_):
        return self._rows

    def mark_executed(self, action_id, result):
        self.executed.append(action_id)

    def mark_failed(self, action_id, error):
        self.executed.append(action_id)


def _row(action_id, *, requires_approval, approved_by):
    from core.response.approval_service import PendingAction

    return PendingAction(
        action_id=action_id,
        action_type="waf_block",
        title="t",
        description="d",
        target="203.0.113.7",
        confidence=0.99,
        reason="r",
        evidence=[],
        created_at="",
        created_by="responder",
        requires_approval=requires_approval,
        status="approved",
        approved_by=approved_by,
    )


def test_the_executor_skips_a_row_no_person_decided():
    service = AutonomousResponseService.__new__(AutonomousResponseService)
    service.approval_service = _Service(
        [
            _row("auto", requires_approval=False, approved_by=None),
            _row("human", requires_approval=True, approved_by="alice"),
        ]
    )
    service._execute_cloudflare_action = lambda **_: {"success": True}

    results = service.execute_approved_actions()

    assert [r["action_id"] for r in results] == ["human"]
    assert service.approval_service.executed == ["human"]
