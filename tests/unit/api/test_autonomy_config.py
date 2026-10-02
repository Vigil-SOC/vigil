"""GET /api/config/autonomy reads the effective daemon flags, not a stored tier."""

from __future__ import annotations

import pytest

from core.response.config import ResponseConfig
from services.api.routers.config import (
    OrchestratorSettingsConfig,
    get_autonomy_config,
)
from services.daemon.config import DaemonConfig

pytestmark = pytest.mark.unit


class _Rows:
    def __init__(self, row):
        self.row = row

    def get_system_config(self, key, default=None):
        if key == "approval.force_manual_approval":
            return self.row
        return default


def test_autonomy_config_reads_both_flags_and_the_db_force_overlay(monkeypatch):
    rows = _Rows({"enabled": True})
    monkeypatch.setattr("services.daemon.intent.get_config_service", lambda: rows)
    base = DaemonConfig()
    base.response = ResponseConfig(
        auto_response_enabled=True, force_manual_approval=False
    )
    monkeypatch.setattr(
        "services.api.routers.config.DaemonConfig.from_env", lambda: base
    )

    forced = get_autonomy_config()
    assert forced.auto_response_enabled is True
    assert forced.force_manual_approval is True
    # the overlay returns a copy; the env value itself stays put
    assert base.response.force_manual_approval is False

    base.response = ResponseConfig(
        auto_response_enabled=False, force_manual_approval=False
    )
    rows.row = None
    plain = get_autonomy_config()
    assert plain.auto_response_enabled is False
    assert plain.force_manual_approval is False

    assert "auto_response_enabled" not in OrchestratorSettingsConfig.model_fields
    assert "force_manual_approval" not in OrchestratorSettingsConfig.model_fields


def test_disabled_db_row_does_not_force_manual(monkeypatch):
    rows = _Rows({"enabled": False})
    monkeypatch.setattr("services.daemon.intent.get_config_service", lambda: rows)
    base = DaemonConfig()
    monkeypatch.setattr(
        "services.api.routers.config.DaemonConfig.from_env", lambda: base
    )

    result = get_autonomy_config()
    assert result.auto_response_enabled is True
    assert result.force_manual_approval is False
