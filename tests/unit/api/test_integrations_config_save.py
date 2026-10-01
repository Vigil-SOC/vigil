"""POST /config/integrations must not report success when a write failed."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.unit


def _payload():
    from services.api.routers.config import IntegrationsConfig

    return IntegrationsConfig(
        enabled_integrations=["splunk"],
        integrations={
            "splunk": {
                "server_url": "https://splunk.example:8089",
                "password": "hunter2",
            }
        },
    )


class _User:
    user_id = "user-1"


def _run(payload, *, set_secret, config_saved=True):
    from services.api.routers import config as config_module

    config_service = MagicMock()
    config_service.set_integration_config.return_value = config_saved
    with (
        patch.object(config_module, "set_secret", set_secret),
        patch.object(config_module, "get_config_service", return_value=config_service),
    ):
        return asyncio.run(
            config_module.set_integrations_config(payload, current_user=_User())
        )


def test_failed_secret_write_is_not_success():
    with pytest.raises(HTTPException) as exc:
        _run(_payload(), set_secret=MagicMock(return_value=False))

    assert exc.value.status_code == 500
    detail = exc.value.detail
    assert isinstance(detail, str)
    assert "splunk" in detail
    assert "password" in detail
    assert "SPLUNK_PASSWORD" in detail
    assert "hunter2" not in detail


def test_failed_integration_config_write_is_not_success():
    with pytest.raises(HTTPException) as exc:
        _run(_payload(), set_secret=MagicMock(return_value=True), config_saved=False)

    assert exc.value.status_code == 500
    detail = exc.value.detail
    assert isinstance(detail, str)
    assert "splunk" in detail
    assert "hunter2" not in detail


def test_empty_secret_is_not_a_failure():
    from services.api.routers.config import IntegrationsConfig

    payload = IntegrationsConfig(
        enabled_integrations=["splunk"],
        integrations={
            "splunk": {
                "server_url": "https://splunk.example:8089",
                "password": "",
            }
        },
    )
    set_secret = MagicMock(return_value=False)
    result = _run(payload, set_secret=set_secret)

    assert result["success"] is True
    set_secret.assert_not_called()
