"""``total_cost_usd`` sums the recorded cost of every finished investigation (#2093)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _seed(*rows: tuple[str, float]) -> None:
    from core.storage.connection import get_db_manager
    from core.storage.models import Investigation

    with get_db_manager().session_scope() as session:
        session.query(Investigation).delete()
        for inv_id, cost in rows:
            session.add(
                Investigation(
                    investigation_id=inv_id,
                    workflow_id="incident-response",
                    trigger_type="manual",
                    workdir=f"/tmp/{inv_id}",
                    status="executing",
                    cost_usd=cost,
                )
            )


def _orchestrator() -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.workdir = MagicMock()
    orch.workdir.read_state.return_value = {}
    orch.shared_intel = MagicMock()
    orch.stats = {
        "investigations_completed": 0,
        "investigations_failed": 0,
        "total_cost_usd": 0.0,
    }
    orch._log_ai_decision = MagicMock()
    orch._send_notification = MagicMock()
    orch._record_progress = MagicMock()
    return orch


async def _reconcile(orch: Orchestrator, inv_id: str, outcome: str) -> None:
    projection = {"status": "terminal", "outcome": outcome, "reason": "done"}
    with patch(
        "services.daemon.orchestrator.read_projection",
        new=AsyncMock(return_value=projection),
    ):
        await orch._reconcile(inv_id)


@pytest.mark.asyncio
async def test_completed_and_failed_runs_add_their_cost_once():
    _seed(("inv-done", 0.10), ("inv-fail", 0.25))
    orch = _orchestrator()

    await _reconcile(orch, "inv-done", "completed")
    await _reconcile(orch, "inv-fail", "error")
    # A later tick that reads the same terminal runs adds nothing.
    await _reconcile(orch, "inv-done", "completed")
    await _reconcile(orch, "inv-fail", "error")

    assert orch.stats["total_cost_usd"] == pytest.approx(0.35)
    assert orch.stats["investigations_completed"] == 1
    assert orch.stats["investigations_failed"] == 1
