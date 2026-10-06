"""Orchestrator settings: flat storage, profiles on GET, Assist/Act on the approval flag."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from core.response.approval_service import APPROVAL_CONFIG_KEY
from services.api.routers.config import (
    ForceManualApprovalConfig,
    OrchestratorSettingsConfig,
    get_force_manual_approval,
    get_orchestrator_config,
    set_force_manual_approval,
    set_orchestrator_config,
)

pytestmark = pytest.mark.unit

_PROFILE_VALUES = {
    "conservative": {
        "max_concurrent_agents": 2,
        "max_iterations_per_agent": 25,
        "max_runtime_per_investigation": 1800,
        "max_cost_per_investigation": 1.0,
        "max_total_hourly_cost": 5.0,
    },
    "balanced": {
        "max_concurrent_agents": 3,
        "max_iterations_per_agent": 50,
        "max_runtime_per_investigation": 3600,
        "max_cost_per_investigation": 5.0,
        "max_total_hourly_cost": 20.0,
    },
    "aggressive": {
        "max_concurrent_agents": 5,
        "max_iterations_per_agent": 100,
        "max_runtime_per_investigation": 7200,
        "max_cost_per_investigation": 15.0,
        "max_total_hourly_cost": 60.0,
    },
}


def test_leftover_auto_assign_severities_is_dropped():
    dumped = OrchestratorSettingsConfig.model_validate(
        {
            "enabled": True,
            "auto_assign_severities": ["medium"],
        }
    ).model_dump()
    assert "auto_assign_severities" not in dumped
    assert dumped["enabled"] is True


def test_get_omits_stale_auto_assign_severities():
    svc = MagicMock()
    svc.get_system_config.return_value = {
        "enabled": True,
        "auto_assign_severities": ["critical", "high", "medium"],
    }
    with patch("services.api.routers.config.get_config_service", return_value=svc):
        result = get_orchestrator_config()
    assert "auto_assign_severities" not in result
    assert result["enabled"] is True


def _store():
    saved: dict = {}
    svc = MagicMock()

    def get_system_config(key, default=None):
        return saved.get(key, default)

    def set_system_config(key, value, **_kwargs):
        saved[key] = value
        return True

    svc.get_system_config.side_effect = get_system_config
    svc.read_system_config.side_effect = lambda key: saved.get(key)
    svc.set_system_config.side_effect = set_system_config
    return saved, svc


def _settings(force: bool, auto: bool):
    settings = MagicMock()
    settings.daemon_force_approval = force
    settings.daemon_auto_response = auto
    return settings


def test_get_profiles_and_post_keeps_the_flat_config():
    saved, svc = _store()
    body = OrchestratorSettingsConfig.model_validate(
        {
            "enabled": True,
            "max_concurrent_agents": 2,
            "profiles": {"balanced": {"label": "Balanced"}},
            "profile": "aggressive",
        }
    )
    with (
        patch("services.api.routers.config.get_config_service", return_value=svc),
        patch("services.api.routers.orchestrator._get_orchestrator", return_value=None),
    ):
        set_orchestrator_config(body, current_user=MagicMock())
        result = get_orchestrator_config()

    stored = saved["orchestrator.settings"]
    assert "profiles" not in stored
    assert "profile" not in stored
    assert "tier" not in stored
    assert stored["enabled"] is True
    assert stored["max_concurrent_agents"] == 2
    assert set(stored) == set(OrchestratorSettingsConfig.model_fields)

    profiles = result["profiles"]
    assert set(profiles) == {"conservative", "balanced", "aggressive"}
    assert profiles["balanced"]["label"] == "Balanced"
    assert profiles["balanced"]["recommended"] is True
    assert profiles["conservative"]["label"] == "Conservative"
    assert profiles["conservative"]["recommended"] is False
    assert profiles["aggressive"]["label"] == "Broad"
    assert profiles["aggressive"]["recommended"] is False
    for key, values in _PROFILE_VALUES.items():
        assert profiles[key]["values"] == values
    assert result["enabled"] is True
    assert result["max_concurrent_agents"] == 2


def test_assist_and_act_write_only_the_approval_flag():
    saved, svc = _store()
    saved["orchestrator.settings"] = {"enabled": True, "max_concurrent_agents": 3}
    user = MagicMock()
    with (
        patch("services.api.routers.config.get_config_service", return_value=svc),
        patch(
            "services.api.routers.config.get_settings",
            return_value=_settings(False, True),
        ),
    ):
        missing = get_force_manual_approval()
        svc.set_system_config.assert_not_called()
        assist = set_force_manual_approval(
            ForceManualApprovalConfig(enabled=True), user
        )
        act = set_force_manual_approval(ForceManualApprovalConfig(enabled=False), user)

    assert missing.enabled is False
    assert missing.environment_wins is False
    assert assist.enabled is True
    assert act.enabled is False
    assert saved[APPROVAL_CONFIG_KEY] == {"enabled": False}
    assert saved["orchestrator.settings"] == {
        "enabled": True,
        "max_concurrent_agents": 3,
    }
    assert "tier" not in saved["orchestrator.settings"]


def test_a_failed_approval_read_is_an_error_not_act():
    svc = MagicMock()
    svc.read_system_config.side_effect = RuntimeError("db down")
    with (
        patch("services.api.routers.config.get_config_service", return_value=svc),
        patch(
            "services.api.routers.config.get_settings",
            return_value=_settings(False, True),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        get_force_manual_approval()

    assert exc.value.status_code == 503


@pytest.mark.parametrize(
    ("force", "auto"),
    [(True, True), (False, False), (True, False)],
)
def test_act_is_refused_when_the_environment_wins(force, auto):
    saved, svc = _store()
    saved[APPROVAL_CONFIG_KEY] = {"enabled": True}
    user = MagicMock()
    with (
        patch("services.api.routers.config.get_config_service", return_value=svc),
        patch(
            "services.api.routers.config.get_settings",
            return_value=_settings(force, auto),
        ),
    ):
        seen = get_force_manual_approval()
        svc.set_system_config.reset_mock()
        with pytest.raises(HTTPException) as exc:
            set_force_manual_approval(ForceManualApprovalConfig(enabled=False), user)
        svc.set_system_config.assert_not_called()
        assist = set_force_manual_approval(
            ForceManualApprovalConfig(enabled=True), user
        )

    assert seen.environment_wins is True
    assert exc.value.status_code == 409
    assert saved[APPROVAL_CONFIG_KEY] == {"enabled": True}
    assert "orchestrator.settings" not in saved
    assert assist.enabled is True
