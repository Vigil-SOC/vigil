"""The supervisor's read side of the heartbeat (issue #963).

``_get_investigations_by_status`` hands the loop dump dicts whose datetimes are
"+00:00" ISO strings, while ``utcnow()`` is naive UTC. Subtracting the two
raised on every tick and killed the whole loop body, so the stale kill never
fired. The write side lives in test_orchestrator_heartbeat.py.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.time import utcnow
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit


def _make_orchestrator(executing_rows) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.config.stale_threshold = 300
    orch.workdir = MagicMock()
    orch._enabled = True
    orch.stats = {"stuck_agents_killed": 0}
    orch._get_investigations_by_status = lambda status: (
        list(executing_rows) if status == "executing" else []
    )
    orch._reconcile = AsyncMock()
    orch._send_notification = MagicMock()
    orch._send_slack_for_notification = AsyncMock()
    orch._update_investigation_status = MagicMock()
    orch._track_hourly_cost = MagicMock()
    orch._check_cross_correlations = AsyncMock()
    return orch


def _dump_row(inv_id: str, idle: timedelta) -> dict:
    # Mirrors InvestigationSchema.dump_many: naive column stamped "+00:00".
    stamp = (utcnow() - idle).isoformat() + "+00:00"
    return {
        "investigation_id": inv_id,
        "status": "executing",
        "last_activity_at": stamp,
        "cost_usd": 0.0,
        "max_cost_usd": 100.0,
    }


def _run_one_iteration(orch: Orchestrator) -> None:
    shutdown = asyncio.Event()

    async def stop_after_first_sleep(event, _seconds):
        event.set()

    orch._sleep = stop_after_first_sleep
    asyncio.run(orch._supervision_loop(shutdown))


def test_stale_executing_row_is_killed(caplog):
    orch = _make_orchestrator([_dump_row("inv-stale", timedelta(seconds=900))])

    with caplog.at_level(logging.ERROR, logger="services.daemon.orchestrator"):
        _run_one_iteration(orch)

    orch._update_investigation_status.assert_called_once_with(
        "inv-stale", "failed", "Stale: no activity"
    )
    assert orch.stats["stuck_agents_killed"] == 1
    assert not any("Supervision loop error" in r.message for r in caplog.records)
    # The rest of the tick must have run too; the raise used to abort it.
    orch._track_hourly_cost.assert_called_once()


def test_fresh_executing_row_is_left_alone(caplog):
    orch = _make_orchestrator([_dump_row("inv-fresh", timedelta(seconds=5))])

    with caplog.at_level(logging.ERROR, logger="services.daemon.orchestrator"):
        _run_one_iteration(orch)

    orch._update_investigation_status.assert_not_called()
    assert orch.stats["stuck_agents_killed"] == 0
    assert not any("Supervision loop error" in r.message for r in caplog.records)
