"""Unit tests for ``services.budget_service`` (#186).

Tests focus on the bypass logic — VK header injection only happens when
should_enforce() returns True, so the priority is making sure that
function never wedges an entire deployment in "no LLM traffic" mode by
accident. Three bypass paths must work: DEV_MODE, LLM_BUDGET_UNLIMITED,
and "no VK configured yet" (bootstrap).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# should_enforce — the gating function
# ---------------------------------------------------------------------------


def test_should_enforce_false_when_dev_mode_on(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")
    with patch(
        "services.budget_service._get_settings", return_value={"default_vk": "sk-bf-x"}
    ):
        from services.budget_service import should_enforce

        assert should_enforce() is False


def test_should_enforce_false_when_unlimited_env_on(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "true")
    with patch(
        "services.budget_service._get_settings", return_value={"default_vk": "sk-bf-x"}
    ):
        from services.budget_service import should_enforce

        assert should_enforce() is False


def test_should_enforce_false_when_no_vk_configured(monkeypatch):
    """Bootstrap window: no VK set → don't try to enforce. The dispatch
    omits the x-bf-vk header and Bifrost's no-VK path applies."""
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")
    with patch(
        "services.budget_service._get_settings", return_value={"default_vk": ""}
    ):
        from services.budget_service import should_enforce

        assert should_enforce() is False


def test_should_enforce_true_when_vk_set_and_no_bypass(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")
    with patch(
        "services.budget_service._get_settings",
        return_value={"default_vk": "sk-bf-real-key"},
    ):
        from services.budget_service import should_enforce

        assert should_enforce() is True


def test_get_active_vk_strips_whitespace():
    """Operator pastes a VK with surrounding whitespace from a config file
    — strip it so the header doesn't get mangled."""
    with patch(
        "services.budget_service._get_settings",
        return_value={"default_vk": "  sk-bf-padded   "},
    ):
        from services.budget_service import get_active_vk

        assert get_active_vk() == "sk-bf-padded"


def test_get_active_vk_returns_none_when_db_unavailable():
    """A misconfigured persistence layer must not block LLM traffic.
    The internal _get_settings catches DB errors and returns None;
    get_active_vk passes that through to the dispatch path which then
    falls back to bootstrap (no-VK) mode."""
    with patch(
        "services.budget_service._get_settings",
        return_value=None,
    ):
        from services.budget_service import get_active_vk

        assert get_active_vk() is None


def test_internal_get_settings_swallows_db_errors():
    """The actual error-handling layer: _get_settings wraps the
    config_service call in try/except so callers can't surface a DB
    failure as a hard error."""
    with patch(
        "database.config_service.get_config_service",
        side_effect=RuntimeError("DB exploded"),
    ):
        from services.budget_service import _get_settings

        # Must not raise.
        result = _get_settings()
        assert result is None


# ---------------------------------------------------------------------------
# get_settings / set_settings — config persistence
# ---------------------------------------------------------------------------


def test_get_settings_returns_normalized_defaults():
    """Empty / missing settings should normalize to safe defaults."""
    with patch("services.budget_service._get_settings", return_value=None):
        from services.budget_service import get_settings

        s = get_settings()
        assert s == {
            "default_vk": "",
            "budget_limit_usd": 0.0,
            "enforcement_mode": "warning",
        }


def test_set_settings_validates_enforcement_mode():
    from services.budget_service import set_settings

    with pytest.raises(ValueError, match="enforcement_mode must be"):
        set_settings(default_vk="sk-bf-x", budget_limit_usd=10.0, enforcement_mode="bogus")


# ---------------------------------------------------------------------------
# BudgetExceeded — typed exception
# ---------------------------------------------------------------------------


def test_budget_exceeded_carries_tier_and_status():
    from services.budget_service import BudgetExceeded

    err = BudgetExceeded(tier="virtual_key", message="$10 spent of $10", status_code=402)
    assert err.tier == "virtual_key"
    assert err.status_code == 402
    assert "10 spent" in str(err)
