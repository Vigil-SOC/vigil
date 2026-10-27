"""``investigations_failed`` counts each investigation that ends failed, once (#2093).

Every failure path writes through ``_update_investigation_status``, so the count
lives there and moves only on the transition into a finished status. These drive
the real writer against a real row, or a second write could not prove the
once-per-investigation rule.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.time import utcnow
from core.workflows.workflows_service import WorkflowsService
from services.daemon import orchestrator as orchestrator_module
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

DEFINITIONS = Path(__file__).resolve().parents[3] / "core" / "workflows" / "definitions"

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _seed(inv_id: str, *, cost: float = 0.0, idle_s: float = 0.0) -> None:
    from core.storage.connection import get_db_manager
    from core.storage.models import Investigation

    with get_db_manager().session_scope() as session:
        session.query(Investigation).delete()
        session.add(
            Investigation(
                investigation_id=inv_id,
                workflow_id="incident-response",
                trigger_type="manual",
                workdir=f"/tmp/{inv_id}",
                status="executing",
                cost_usd=cost,
                max_cost_usd=1.0,
                last_activity_at=utcnow() - timedelta(seconds=idle_s),
            )
        )


def _orchestrator() -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig(stale_threshold=300)
    orch.workdir = MagicMock()
    orch._enabled = True
    orch.stats = {
        "investigations_failed": 0,
        "total_cost_usd": 0.0,
        "stuck_agents_killed": 0,
    }
    orch._reconcile = AsyncMock()
    orch._send_notification = MagicMock()
    orch._send_slack_for_notification = AsyncMock()
    orch._check_cross_correlations = AsyncMock()
    return orch


def _supervise_once(orch: Orchestrator) -> None:
    async def stop(event, _seconds):
        event.set()

    orch._sleep = stop
    asyncio.run(orch._supervision_loop(asyncio.Event()))


@pytest.fixture
def otel_failed(monkeypatch):
    counter = MagicMock()
    monkeypatch.setattr(orchestrator_module, "_inv_failed", counter)
    return counter


def test_a_stale_kill_counts_one(otel_failed):
    _seed("inv-stale", idle_s=900)
    orch = _orchestrator()
    _supervise_once(orch)
    assert orch.stats["investigations_failed"] == 1
    otel_failed.add.assert_called_once_with(1)


def test_a_budget_kill_counts_one():
    _seed("inv-budget", cost=1.5)
    orch = _orchestrator()
    _supervise_once(orch)
    assert orch.stats["investigations_failed"] == 1
    assert orch.stats["total_cost_usd"] == pytest.approx(1.5)


def test_stale_and_over_budget_in_one_tick_counts_once(otel_failed):
    _seed("inv-both", cost=1.5, idle_s=900)
    orch = _orchestrator()
    _supervise_once(orch)
    assert orch.stats["investigations_failed"] == 1
    assert orch.stats["total_cost_usd"] == pytest.approx(1.5)
    otel_failed.add.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_an_enqueue_failure_counts_one():
    _seed("inv-enqueue")
    orch = _orchestrator()
    orch._workflows = WorkflowsService(workflows_dir=DEFINITIONS)
    orch.workdir.read_file.return_value = None
    with (
        patch(
            "services.daemon.orchestrator.WorkflowRunService",
            return_value=MagicMock(**{"get_run.return_value": None}),
        ),
        patch(
            "services.daemon.orchestrator.enqueue_run",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
    ):
        await orch._enqueue_investigation(
            {"investigation_id": "inv-enqueue", "workflow_id": "incident-response"}
        )
    assert orch.stats["investigations_failed"] == 1


@pytest.mark.asyncio
async def test_an_agent_error_counts_one_and_a_retry_adds_nothing():
    _seed("inv-agent", cost=0.2)
    orch = _orchestrator()
    del orch._reconcile  # the real one, reading a terminal projection
    orch._record_progress = MagicMock()
    projection = {"status": "terminal", "outcome": "error", "reason": "boom"}
    with patch(
        "services.daemon.orchestrator.read_projection",
        new=AsyncMock(return_value=projection),
    ):
        await orch._reconcile("inv-agent")
        await orch._reconcile("inv-agent")
    orch._abandon_in_flight()  # already finished, so not in flight
    assert orch.stats["investigations_failed"] == 1
    assert orch.stats["total_cost_usd"] == pytest.approx(0.2)


def test_abandoning_an_in_flight_run_counts_one():
    _seed("inv-abandon")
    orch = _orchestrator()
    orch._abandon_in_flight()
    assert orch.stats["investigations_failed"] == 1
