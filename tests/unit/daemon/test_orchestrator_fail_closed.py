"""An unreadable count or spend holds intake instead of lifting the limits."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.daemon import orchestrator as orchestrator_module
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit


def _orchestrator() -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig(max_concurrent_agents=3)
    orch._queued_intake_triggers = MagicMock(
        return_value=[{"id": n, "kind": "detection"} for n in range(10)]
    )
    orch._resolve_intake_row = lambda row, _now: row
    orch._process_intake_row = AsyncMock()
    orch._queued_intake_depth = MagicMock(return_value=None)
    orch._schedule_runs_in_flight = MagicMock(return_value=0)
    orch._hourly_budget_exhausted = MagicMock(return_value=False)
    return orch


def _count_raises(monkeypatch) -> None:
    def boom() -> int:
        raise RuntimeError("statement timeout")

    monkeypatch.setattr(orchestrator_module, "_count_investigations_in_flight", boom)
    monkeypatch.setattr(orchestrator_module, "_count_investigations_running", boom)


def test_failed_in_flight_count_reads_as_full(monkeypatch):
    _count_raises(monkeypatch)
    orch = _orchestrator()
    assert orch._in_flight() == orch.config.max_concurrent_agents


def test_failed_running_count_reads_as_full(monkeypatch):
    _count_raises(monkeypatch)
    orch = _orchestrator()
    assert orch._running() == orch.config.max_concurrent_agents


@pytest.mark.asyncio
async def test_failed_in_flight_count_launches_nothing(monkeypatch):
    _count_raises(monkeypatch)
    orch = _orchestrator()
    await orch._drain_intake(None)
    orch._process_intake_row.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_running_count_resumes_nothing(monkeypatch):
    _count_raises(monkeypatch)
    orch = _orchestrator()
    orch._get_investigations_by_status = MagicMock(
        return_value=[{"investigation_id": "inv-1"}]
    )
    orch._update_investigation_status = MagicMock()
    orch._enqueue_investigation = AsyncMock()

    await orch._pickup_assigned_investigations(None)

    orch._enqueue_investigation.assert_not_awaited()


@pytest.mark.asyncio
async def test_readable_count_still_admits_up_to_the_cap(monkeypatch):
    launched = {"n": 0}
    monkeypatch.setattr(
        orchestrator_module, "_count_investigations_in_flight", lambda: launched["n"]
    )
    monkeypatch.setattr(
        orchestrator_module, "_count_investigations_running", lambda: launched["n"]
    )
    orch = _orchestrator()

    async def launch(row, _shutdown):
        launched["n"] += 1

    orch._process_intake_row = AsyncMock(side_effect=launch)
    await orch._drain_intake(None)
    assert orch._process_intake_row.await_count == 3


@pytest.mark.asyncio
async def test_assigned_rows_filling_the_cap_do_not_block_drain(monkeypatch):
    # Cap-many rows sit in `assigned` (the dry-run shape, #1874): they fill
    # the in-flight count but none is running, so intake still launches.
    launched = {"n": 0}
    monkeypatch.setattr(
        orchestrator_module, "_count_investigations_in_flight", lambda: 3
    )
    monkeypatch.setattr(
        orchestrator_module, "_count_investigations_running", lambda: launched["n"]
    )
    orch = _orchestrator()

    async def launch(row, _shutdown):
        launched["n"] += 1

    orch._process_intake_row = AsyncMock(side_effect=launch)
    await orch._drain_intake(None)

    assert orch._process_intake_row.await_count == 3


def test_unknown_spend_is_paused_even_on_a_cold_start():
    orch = _orchestrator()
    assert orch._hourly_pause_decision(None, 5.0) is True
    assert orch._hourly_pause_decision(1.0, 5.0) is False
    assert orch._hourly_pause_decision(5.0, 5.0) is True
