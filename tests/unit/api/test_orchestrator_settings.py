"""The Auto Investigate settings surface no longer carries auto_assign_severities (#975)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.api.routers.config import (
    OrchestratorSettingsConfig,
    get_orchestrator_config,
)

pytestmark = pytest.mark.unit


def test_leftover_auto_assign_severities_is_dropped():
    dumped = OrchestratorSettingsConfig.model_validate(
        {
            "enabled": True,
            "auto_assign_severities": ["medium"],
        }
    ).model_dump()
    assert "auto_assign_severities" not in dumped
    assert dumped["enabled"] is True


@pytest.mark.asyncio
async def test_get_omits_stale_auto_assign_severities():
    svc = MagicMock()
    svc.get_system_config.return_value = {
        "enabled": True,
        "auto_assign_severities": ["critical", "high", "medium"],
    }
    with patch("services.api.routers.config.get_config_service", return_value=svc):
        result = await get_orchestrator_config()
    assert "auto_assign_severities" not in result
    assert result["enabled"] is True
