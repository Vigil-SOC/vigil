"""Wake starts a fresh run for a failed investigation and hands it back to the daemon."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from services.api.routers import orchestrator as router

pytestmark = pytest.mark.unit


def _orch(status: str) -> MagicMock:
    orch = MagicMock()
    orch.get_investigation.return_value = {"investigation_id": "inv-1", "status": status}
    return orch


@pytest.mark.parametrize("status", ["failed", "completed"])
def test_wake_gives_the_investigation_a_new_run_then_requeues_it(status):
    orch = _orch(status)
    with patch.object(router, "_get_orchestrator", return_value=orch):
        result = router.wake_investigation("inv-1")

    orch.wake.assert_called_once_with("inv-1")
    orch._update_investigation_status.assert_called_once_with("inv-1", "assigned")
    assert result["success"] is True


def test_wake_refuses_an_investigation_that_is_still_running():
    orch = _orch("executing")
    with patch.object(router, "_get_orchestrator", return_value=orch):
        with pytest.raises(HTTPException) as err:
            router.wake_investigation("inv-1")

    assert err.value.status_code == 400
    orch.wake.assert_not_called()
