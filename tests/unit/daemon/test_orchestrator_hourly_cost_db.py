"""The hourly cost ceiling, read from investigation rows (#1018).

The window used to be an in-memory list nothing appended to, so the cap never
bound. It is now the sum of ``cost_usd`` over rows whose ``last_activity_at``
falls in the last hour, and intake stops launching while that sum is at the cap.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.time import utcnow
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

CAP = 5.0


def _seed(*rows: tuple[str, float, timedelta], saved_cap: float | None = None) -> None:
    """Replace every investigation with ``(id, cost_usd, idle)`` completed rows."""
    from core.storage.connection import get_db_manager
    from core.storage.models import Investigation, SystemConfig

    now = utcnow()
    with get_db_manager().session_scope() as session:
        session.query(Investigation).delete()
        session.query(SystemConfig).filter_by(key="orchestrator.settings").delete()
        if saved_cap is not None:
            session.add(
                SystemConfig(
                    key="orchestrator.settings",
                    value={"enabled": True, "max_total_hourly_cost": saved_cap},
                )
            )
        for inv_id, cost, idle in rows:
            session.add(
                Investigation(
                    investigation_id=inv_id,
                    workflow_id="incident-response",
                    trigger_type="manual",
                    workdir=f"/tmp/{inv_id}",
                    status="completed",
                    cost_usd=cost,
                    last_activity_at=now - idle,
                    completed_at=now - idle,
                )
            )


def _orchestrator() -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig(max_total_hourly_cost=CAP)
    orch._queued_intake_triggers = MagicMock(return_value=[{"id": 1}])
    orch._resolve_intake_row = lambda row, _now: row
    orch._process_intake_row = AsyncMock()
    orch._queued_intake_depth = MagicMock(return_value=None)
    orch._in_flight = MagicMock(return_value=0)
    orch._update_investigation_status = MagicMock()
    orch._enqueue_investigation = AsyncMock()
    return orch


async def _run_intake(orch: Orchestrator, assigned: list) -> None:
    by_status = {"assigned": assigned}
    orch._get_investigations_by_status = lambda status: by_status.get(status, [])
    await orch._drain_intake(None)
    await orch._pickup_assigned_investigations(None)


@pytest.mark.asyncio
async def test_intake_pauses_when_the_hour_reaches_the_cap():
    _seed(
        ("inv-hour-a", 3.0, timedelta(minutes=10)),
        ("inv-hour-b", 2.5, timedelta(minutes=40)),
    )
    orch = _orchestrator()

    await _run_intake(orch, [{"investigation_id": "inv-assigned"}])

    orch._process_intake_row.assert_not_awaited()
    orch._enqueue_investigation.assert_not_awaited()
    summary = orch.get_cost_summary()
    assert summary["hourly_cost_usd"] == 5.5
    assert summary["hourly_budget_remaining"] == -0.5


@pytest.mark.asyncio
async def test_cost_older_than_an_hour_falls_out_and_intake_launches():
    _seed(
        ("inv-hour-a", 3.0, timedelta(minutes=10)),
        ("inv-hour-old", 2.5, timedelta(minutes=61)),
    )
    orch = _orchestrator()

    await _run_intake(orch, [{"investigation_id": "inv-assigned"}])

    orch._process_intake_row.assert_awaited_once()
    orch._enqueue_investigation.assert_awaited_once()
    assert orch.get_cost_summary()["hourly_cost_usd"] == 3.0


@pytest.mark.asyncio
async def test_the_saved_cap_wins_over_startup_config():
    # The API's status payload runs on default config; both must read Settings.
    _seed(
        ("inv-hour-a", 3.0, timedelta(minutes=10)),
        ("inv-hour-b", 2.5, timedelta(minutes=40)),
        saved_cap=10.0,
    )
    orch = _orchestrator()

    await _run_intake(orch, [{"investigation_id": "inv-assigned"}])

    orch._process_intake_row.assert_awaited_once()
    assert orch.get_cost_summary()["hourly_budget_remaining"] == 4.5


@pytest.mark.asyncio
async def test_an_unreadable_hour_keeps_intake_paused():
    _seed(("inv-hour-a", 6.0, timedelta(minutes=10)))
    orch = _orchestrator()
    assert orch._hourly_budget_exhausted() is True

    orch._hourly_cost = MagicMock(return_value=None)
    await _run_intake(orch, [{"investigation_id": "inv-assigned"}])

    orch._process_intake_row.assert_not_awaited()
    orch._enqueue_investigation.assert_not_awaited()
