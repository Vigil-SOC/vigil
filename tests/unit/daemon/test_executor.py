"""The approved-action executor runs on its own clock, held only by Stop."""

import asyncio
from unittest.mock import Mock

import pytest

from core.response.approval_service import PendingAction
from core.response.autonomous_response_service import AutonomousResponseService
from services.daemon.executor import ApprovedActionExecutor

pytestmark = pytest.mark.unit


class _Approvals:
    def __init__(self, rows):
        self._rows = rows
        self.done = []

    def list_actions(self, status=None, **_):
        return self._rows

    def mark_executed(self, action_id, result):
        self.done.append(action_id)

    def mark_failed(self, action_id, error):
        self.done.append(action_id)


def _approved(action_id="a-1"):
    return PendingAction(
        action_id=action_id,
        action_type="waf_block",
        title="t",
        description="d",
        target="203.0.113.7",
        confidence=0.5,
        reason="r",
        evidence=[],
        created_at="",
        created_by="agent",
        requires_approval=True,
        status="approved",
        approved_by="alice",
    )


def _service(monkeypatch, *, held):
    monkeypatch.setattr(
        "core.response.autonomous_response_service.execution_held", lambda: held
    )
    service = AutonomousResponseService.__new__(AutonomousResponseService)
    service.approval_service = _Approvals([_approved()])
    service._execute_cloudflare_action = lambda **_: {"success": True}
    return service


async def _one_pass(executor):
    """Let the loop run once, then stop it."""
    shutdown = asyncio.Event()
    task = asyncio.create_task(executor.run(shutdown))
    await asyncio.sleep(0.05)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_an_approved_action_runs_with_auto_response_off(monkeypatch):
    service = _service(monkeypatch, held=False)
    service.config = Mock(auto_response_enabled=False)  # no longer a gate
    executor = ApprovedActionExecutor(service)

    await _one_pass(executor)

    assert service.approval_service.done == ["a-1"]
    assert executor.stats == {"executed": 1, "failed": 0, "errors": 0}


@pytest.mark.asyncio
async def test_stop_holds_an_approved_action(monkeypatch):
    service = _service(monkeypatch, held=True)
    executor = ApprovedActionExecutor(service)

    await _one_pass(executor)

    assert service.approval_service.done == []
    assert executor.stats == {"executed": 0, "failed": 0, "errors": 0}


@pytest.mark.asyncio
async def test_a_failed_execution_is_counted(monkeypatch):
    service = _service(monkeypatch, held=False)
    service._execute_cloudflare_action = lambda **_: {"success": False, "error": "x"}
    executor = ApprovedActionExecutor(service)

    await _one_pass(executor)

    assert executor.stats["failed"] == 1 and executor.stats["executed"] == 0


@pytest.mark.asyncio
async def test_an_error_is_counted_and_the_loop_survives():
    service = Mock()
    service.execute_approved_actions.side_effect = RuntimeError("db down")
    executor = ApprovedActionExecutor(service)

    await _one_pass(executor)

    assert executor.stats["errors"] == 1
