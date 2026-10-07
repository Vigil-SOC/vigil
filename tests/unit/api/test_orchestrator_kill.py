"""Kill all reaches the daemon through the persisted flag it polls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from services.api.routers import orchestrator as router

pytestmark = pytest.mark.unit

USER = SimpleNamespace(user_id="u-1")


@pytest.mark.asyncio
async def test_kill_persists_disabled_before_failing_in_flight_records():
    order: list[str] = []
    orch = MagicMock()
    orch.kill = AsyncMock(side_effect=lambda: order.append("kill"))
    with (
        patch.object(
            router,
            "_persist_orchestrator_enabled",
            side_effect=lambda *a: order.append(("persist", a)),
        ),
        patch.object(router, "_get_orchestrator", return_value=orch),
    ):
        result = await router.kill_orchestrator(current_user=USER)

    assert order[0] == ("persist", (False, "u-1", "Orchestrator killed via API"))
    assert order[1] == "kill"
    assert result["enabled"] is False


@pytest.mark.asyncio
async def test_kill_that_cannot_reach_the_daemon_is_an_error():
    orch = MagicMock()
    orch.kill = AsyncMock()
    with (
        patch.object(
            router,
            "_persist_orchestrator_enabled",
            side_effect=RuntimeError("db at 10.0.0.5 down"),
        ),
        patch.object(router, "_get_orchestrator", return_value=orch),
    ):
        with pytest.raises(HTTPException) as err:
            await router.kill_orchestrator(current_user=USER)

    assert err.value.status_code == 500
    assert "10.0.0.5" not in err.value.detail
    orch.kill.assert_not_awaited()
