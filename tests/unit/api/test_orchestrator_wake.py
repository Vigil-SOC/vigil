"""Restart queues a new investigation; it never re-runs the finished one."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from core.auth import tool_principal
from services.api.routers import orchestrator as router

pytestmark = pytest.mark.unit


def _orch(status: str, hypotheses: str = "lateral movement via svc-backup"):
    orch = MagicMock()
    orch.get_investigation.return_value = {
        "investigation_id": "inv-old",
        "status": status,
        "workflow_id": "incident-response",
        "case_id": "case-1",
        "trigger_ids": ["f-1", "f-2"],
        "priority": "high",
    }
    orch.workdir.read_file.return_value = hypotheses
    return orch


@pytest.mark.parametrize("status", ["failed", "completed"])
def test_wake_queues_a_new_human_ask_on_the_same_case(status):
    orch = _orch(status)
    with (
        patch.object(router, "_get_orchestrator", return_value=orch),
        patch("services.daemon.orchestrator.insert_intake_trigger") as insert,
    ):
        result = router.wake_investigation(
            "inv-old", user=SimpleNamespace(username="nestor")
        )

    payload = insert.call_args.kwargs["payload"]
    insert.assert_called_once()
    assert insert.call_args.kwargs["kind"] == "human_ask"
    assert insert.call_args.kwargs["priority"] == "high"
    assert payload["workflow_id"] == "incident-response"
    assert payload["case_id"] == "case-1"
    assert payload["finding_ids"] == ["f-1", "f-2"]
    assert payload["hypothesis"] == "lateral movement via svc-backup"
    assert payload["started_by"] == "nestor"
    assert tool_principal.verify(payload["principal"]) == "nestor"
    assert result["case_id"] == "case-1"
    assert "case-1" in result["message"]
    # the old investigation's failure stays on record
    orch._update_investigation_status.assert_not_called()
    orch.workdir.write_state.assert_not_called()
    orch.workdir.read_state.assert_not_called()


def test_wake_sends_no_hypothesis_when_the_file_is_empty():
    orch = _orch("failed", hypotheses="")
    with (
        patch.object(router, "_get_orchestrator", return_value=orch),
        patch("services.daemon.orchestrator.insert_intake_trigger") as insert,
    ):
        router.wake_investigation("inv-old", user=SimpleNamespace(username="nestor"))

    assert insert.call_args.kwargs["payload"]["hypothesis"] is None


@pytest.mark.parametrize("status", ["executing", "assigned", "sleeping"])
def test_wake_rejects_unfinished_investigations(status):
    orch = _orch(status)
    with (
        patch.object(router, "_get_orchestrator", return_value=orch),
        patch("services.daemon.orchestrator.insert_intake_trigger") as insert,
    ):
        with pytest.raises(HTTPException) as err:
            router.wake_investigation("inv-old")

    assert err.value.status_code == 400
    insert.assert_not_called()
