"""Orchestrator settings: flat storage, profiles on GET."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from services.api.routers.config import (
    OrchestratorConfigResponse,
    OrchestratorSettingsConfig,
    get_orchestrator_config,
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
    with patch("services.api.routers.config.get_config_service", return_value=svc):
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


def test_get_serves_defaults_and_bounds_that_match_the_model():
    svc = MagicMock()
    svc.get_system_config.return_value = None
    with patch("services.api.routers.config.get_config_service", return_value=svc):
        result = get_orchestrator_config()

    # The route's response model is what the client receives
    served = OrchestratorConfigResponse.model_validate(result).model_dump()
    assert served["defaults"] == OrchestratorSettingsConfig().model_dump()
    assert served["defaults"]["enabled"] is False
    props = OrchestratorSettingsConfig.model_json_schema()["properties"]
    numeric = {k for k, p in props.items() if "minimum" in p}
    assert set(served["bounds"]) == numeric
    for name in numeric:
        b = served["bounds"][name]
        assert (b["min"], b["max"]) == (props[name]["minimum"], props[name]["maximum"])
        assert b["min"] > 0 and b["step"] > 0
        assert b["min"] <= served["defaults"][name] <= b["max"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_concurrent_agents", 0),
        ("max_concurrent_agents", 11),
        ("max_iterations_per_agent", 0),
        ("max_cost_per_investigation", 0),
        ("max_total_hourly_cost", 501),
        ("stale_threshold", 59),
        ("workdir_base", ""),
    ],
)
def test_post_rejects_out_of_bounds_values(field, value):
    with pytest.raises(ValidationError):
        OrchestratorSettingsConfig.model_validate({field: value})


def test_a_config_saved_before_the_bounds_still_loads_and_can_be_corrected():
    saved, svc = _store()
    # "Unlimited" used to be stored as 0
    saved["orchestrator.settings"] = {
        "max_concurrent_agents": 0,
        "max_cost_per_investigation": 0,
    }
    with patch("services.api.routers.config.get_config_service", return_value=svc):
        result = OrchestratorConfigResponse.model_validate(get_orchestrator_config())
        assert result.max_concurrent_agents == 0
        set_orchestrator_config(
            OrchestratorSettingsConfig.model_validate(
                {
                    **result.model_dump(),
                    "max_concurrent_agents": 1,
                    "max_cost_per_investigation": 1,
                }
            ),
            current_user=MagicMock(),
        )

    assert saved["orchestrator.settings"]["max_concurrent_agents"] == 1
