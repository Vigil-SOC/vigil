"""GET /api/config/autonomy is Assist exactly when effective act_reversible is off."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.response.ceiling import CEILING_KEY
from services.api.routers.config import (
    OrchestratorSettingsConfig,
    get_autonomy_config,
)

pytestmark = pytest.mark.unit


class _Rows:
    def __init__(self, row):
        self.row = row

    def read_system_config(self, key):
        assert key == CEILING_KEY
        return self.row


def _settings(force: bool, auto: bool):
    return SimpleNamespace(daemon_force_approval=force, daemon_auto_response=auto)


def _ceiling(**flags):
    base = {
        "act_reversible": False,
        "act_irreversible": False,
        "stopped": False,
        "four_eyes": False,
    }
    base.update(flags)
    return base


def test_chip_follows_effective_act_reversible(monkeypatch):
    rows = _Rows(_ceiling(act_reversible=False))
    monkeypatch.setattr(
        "core.storage.config_service.get_config_service", lambda **_k: rows
    )
    monkeypatch.setattr(
        "services.api.routers.config.get_settings",
        lambda: _settings(False, True),
    )

    assist = get_autonomy_config()
    assert assist.auto_response_enabled is True
    assert assist.force_manual_approval is True

    rows.row = _ceiling(act_reversible=True)
    act = get_autonomy_config()
    assert act.auto_response_enabled is True
    assert act.force_manual_approval is False

    rows.row = _ceiling(act_reversible=True, stopped=True)
    stopped = get_autonomy_config()
    assert stopped.force_manual_approval is True

    monkeypatch.setattr(
        "services.api.routers.config.get_settings",
        lambda: _settings(True, True),
    )
    rows.row = _ceiling(act_reversible=True)
    locked = get_autonomy_config()
    assert locked.force_manual_approval is True

    assert "auto_response_enabled" not in OrchestratorSettingsConfig.model_fields
    assert "force_manual_approval" not in OrchestratorSettingsConfig.model_fields
    assert "act_reversible" not in OrchestratorSettingsConfig.model_fields


def test_unreadable_ceiling_is_assist(monkeypatch):
    class _Down:
        def read_system_config(self, key):
            raise RuntimeError("db down")

    monkeypatch.setattr(
        "core.storage.config_service.get_config_service", lambda **_k: _Down()
    )
    result = get_autonomy_config()
    assert result.auto_response_enabled is True
    assert result.force_manual_approval is True
