"""POST /config/integrations must not report success when a write failed."""

from __future__ import annotations

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
        return config_module.set_integrations_config(payload, current_user=_User())


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


def _save(stored_config, incoming, *, secret_set):
    from services.api.routers import config as config_module
    from services.api.routers.config import IntegrationsConfig

    config_service = MagicMock()
    config_service.get_integration_config.return_value = {"config": stored_config}
    set_secret = MagicMock(return_value=True)
    payload = IntegrationsConfig(
        enabled_integrations=["splunk"], integrations={"splunk": incoming}
    )
    with (
        patch.object(config_module, "set_secret", set_secret),
        patch.object(config_module, "get_config_service", return_value=config_service),
        patch(
            "core.integrations.integration_secrets.get_secret",
            side_effect=lambda env: (
                "stored-pw" if secret_set and env == "SPLUNK_PASSWORD" else ""
            ),
        ),
    ):
        result = config_module.set_integrations_config(payload, current_user=_User())
    return result, config_service, set_secret


def test_moving_the_server_url_without_the_credential_is_refused():
    stored = {"server_url": "https://splunk.example:8089", "username": "svc"}
    incoming = {
        "server_url": "https://attacker.example",
        "username": "svc",
        "password": "",
    }

    with pytest.raises(HTTPException) as exc:
        _save(stored, incoming, secret_set=True)

    assert exc.value.status_code == 400
    assert "password" in exc.value.detail


def test_moving_the_server_url_with_the_credential_is_saved():
    stored = {"server_url": "https://splunk.example:8089"}
    incoming = {"server_url": "https://new.example:8089", "password": "fresh"}

    result, _, set_secret = _save(stored, incoming, secret_set=True)

    assert result["success"] is True
    set_secret.assert_called_once_with("SPLUNK_PASSWORD", "fresh")


def test_editing_other_fields_keeps_the_stored_credential():
    stored = {"server_url": "https://splunk.example:8089", "lookback_hours": 1}
    incoming = {
        "server_url": "https://splunk.example:8089/",
        "lookback_hours": 6,
        "password": "",
    }

    result, config_service, _ = _save(stored, incoming, secret_set=True)

    assert result["success"] is True
    config_service.set_integration_config.assert_called_once()


def test_a_first_time_url_with_no_stored_credential_is_saved():
    result, _, _ = _save(
        {},
        {"server_url": "https://splunk.example:8089", "password": ""},
        secret_set=False,
    )

    assert result["success"] is True
