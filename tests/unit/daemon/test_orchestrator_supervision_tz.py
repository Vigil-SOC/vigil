"""_supervision_loop reads last_activity_at back through InvestigationSchema,
whose IsoDateTime field always stamps a naive value aware (core/storage/schemas
base.py's _iso_utc, when_used="json"). ``now`` stays naive (core.time.utcnow).
Comparing the two used to raise every tick, before the stale-kill check ever ran.
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

from core.storage.schemas.base import _iso_utc
from core.time import utcnow
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

pytestmark = pytest.mark.unit

INV = "inv-20260917-tzcase1"


def _orchestrator(last_activity_at):
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch._enabled = True
    orch.stats = {"stuck_agents_killed": 0}

    executing = [
        {
            "investigation_id": INV,
            "last_activity_at": last_activity_at,
            "cost_usd": 0.0,
            "iteration_count": 1,
            "current_activity": "polling",
        }
    ]

    def by_status(status):
        return executing if status == "executing" else []

    orch._get_investigations_by_status = MagicMock(side_effect=by_status)
    orch._reconcile = AsyncMock()
    orch._send_notification = MagicMock()
    orch._send_slack_for_notification = AsyncMock()
    orch._update_investigation_status = MagicMock()
    orch._check_cross_correlations = AsyncMock()
    orch._track_hourly_cost = MagicMock()

    shutdown_event = asyncio.Event()

    # `_sleep` runs whether the tick above raised or returned, so it is the
    # one call this test can rely on to end the loop after exactly one pass.
    async def stop_after_one_tick(event, seconds):
        shutdown_event.set()

    orch._sleep = AsyncMock(side_effect=stop_after_one_tick)
    return orch, shutdown_event


async def _run_one_tick(orch, shutdown_event):
    await asyncio.wait_for(orch._supervision_loop(shutdown_event), timeout=5)


def test_stale_kill_fires_on_an_aware_last_activity_at():
    # This is the shape InvestigationSchema.dump_many() actually returns: a
    # naive DB column, serialized through the schema's IsoDateTime field.
    stale = utcnow() - timedelta(seconds=OrchestratorConfig().stale_threshold + 1)
    aware_stale = _iso_utc(stale)
    assert aware_stale.endswith("+00:00")

    orch, shutdown_event = _orchestrator(aware_stale)
    asyncio.run(_run_one_tick(orch, shutdown_event))

    orch._update_investigation_status.assert_called_once_with(
        INV, "failed", "Stale: no activity"
    )
    assert orch.stats["stuck_agents_killed"] == 1


def test_fresh_last_activity_at_does_not_raise(caplog):
    fresh = _iso_utc(utcnow())

    orch, shutdown_event = _orchestrator(fresh)
    with caplog.at_level(logging.ERROR, logger="services.daemon.orchestrator"):
        asyncio.run(_run_one_tick(orch, shutdown_event))

    # On main the mismatch fires on the subtraction itself, so every executing
    # investigation crashed the tick, not only the ones actually stale.
    assert not any("Supervision loop error" in r.message for r in caplog.records)
    orch._update_investigation_status.assert_not_called()
    assert orch.stats["stuck_agents_killed"] == 0
