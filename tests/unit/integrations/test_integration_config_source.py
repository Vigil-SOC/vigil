"""Runtime integration config is read from the database, not the JSON mirror.

The console writes ``integration_configs``. ``integrations_config.json`` is a
throwaway copy, and on a compose daemon or a restarted Helm pod it is missing.
``get_integration_config`` used to read only that file, so non-secret fields
resolved to nothing while the console showed them saved.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from core.config import get_integration_config, is_integration_enabled
from core.integrations.integration_bridge_service import IntegrationBridgeService

pytestmark = pytest.mark.unit

_SPLUNK = {
    "server_url": "https://splunk.example:8089",
    "username": "svc",
    "verify_ssl": False,
}


def _patch_db(monkeypatch, service: MagicMock) -> None:
    monkeypatch.setattr(
        "core.storage.config_service.get_config_service",
        lambda user_id="system": service,
    )


def test_db_rows_resolve_when_file_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    service = MagicMock()
    service.list_integrations.return_value = [
        {"integration_id": "splunk", "enabled": True, "config": dict(_SPLUNK)}
    ]
    _patch_db(monkeypatch, service)

    assert not (tmp_path / "integrations_config.json").exists()
    assert is_integration_enabled("splunk") is True
    assert get_integration_config("splunk") == _SPLUNK

    loaded = IntegrationBridgeService().load_integration_config()
    assert loaded["enabled_integrations"] == ["splunk"]
    assert loaded["integrations"]["splunk"] == _SPLUNK


def test_disabled_db_row_returns_empty_config(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    # A stale mirror that still lists Splunk as enabled must not win.
    (tmp_path / "integrations_config.json").write_text(
        json.dumps(
            {
                "enabled_integrations": ["splunk"],
                "integrations": {"splunk": {"server_url": "https://stale.example"}},
            }
        )
    )
    service = MagicMock()
    service.list_integrations.return_value = [
        {"integration_id": "splunk", "enabled": False, "config": dict(_SPLUNK)}
    ]
    _patch_db(monkeypatch, service)

    assert is_integration_enabled("splunk") is False
    assert get_integration_config("splunk") == {}


@pytest.mark.parametrize("failure", ["empty", "raising"])
def test_file_fallback_when_db_empty_or_raising(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    (tmp_path / "integrations_config.json").write_text(
        json.dumps(
            {"enabled_integrations": ["splunk"], "integrations": {"splunk": _SPLUNK}}
        )
    )
    service = MagicMock()
    if failure == "empty":
        service.list_integrations.return_value = []
    else:
        service.list_integrations.side_effect = RuntimeError("db down")
    _patch_db(monkeypatch, service)

    assert is_integration_enabled("splunk") is True
    assert get_integration_config("splunk") == _SPLUNK
