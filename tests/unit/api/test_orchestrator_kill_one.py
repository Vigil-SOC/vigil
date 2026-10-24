"""Killing one investigation fails a live row and leaves anything else alone."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from core.storage.connection import get_db_manager
from core.storage.models import Investigation
from services.api.routers import orchestrator as router

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _row(status: str) -> str:
    inv_id = f"inv-{uuid.uuid4().hex[:12]}"
    with get_db_manager().session_scope() as session:
        session.add(
            Investigation(
                investigation_id=inv_id,
                workflow_id="incident-response",
                trigger_type="human_ask",
                status=status,
                workdir=f"/tmp/{inv_id}",
                master_review_notes="verdict: benign",
                current_activity="calling a tool",
            )
        )
    return inv_id


def _read(inv_id: str):
    with get_db_manager().session_scope() as session:
        inv = session.get(Investigation, inv_id)
        return inv.status, inv.master_review_notes, inv.current_activity


def _kill(inv_id: str) -> tuple[dict | HTTPException, MagicMock]:
    orch = MagicMock()
    orch.workdir.read_state.return_value = {}
    with patch.object(router, "_get_orchestrator", return_value=orch):
        try:
            return router.kill_investigation(inv_id), orch
        except HTTPException as err:
            return err, orch


@pytest.mark.parametrize(
    "status", ["assigned", "executing", "waiting_approval", "paused"]
)
def test_kill_fails_a_live_investigation(status):
    inv_id = _row(status)

    result, orch = _kill(inv_id)

    assert result["success"] is True
    assert _read(inv_id) == ("failed", "Manually killed by user", None)
    orch.workdir.write_state.assert_called_once_with(
        inv_id, {"status": "failed", "failure_reason": "Manually killed by user"}
    )


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_kill_leaves_a_finished_investigation_and_its_verdict(status):
    inv_id = _row(status)

    err, orch = _kill(inv_id)

    assert err.status_code == 409
    assert _read(inv_id) == (status, "verdict: benign", "calling a tool")
    orch.workdir.write_state.assert_not_called()


def test_kill_of_an_unknown_id_is_404_and_writes_no_workdir():
    err, orch = _kill("inv-does-not-exist")

    assert err.status_code == 404
    orch.workdir.write_state.assert_not_called()
