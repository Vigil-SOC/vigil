"""Unit tests for ``core.llm.cost.budget`` (#186).

Tests focus on the bypass logic — VK header injection only happens when
should_enforce() returns True, so the priority is making sure that
function never wedges an entire deployment in "no LLM traffic" mode by
accident. Three bypass paths must work: DEV_MODE, LLM_BUDGET_UNLIMITED,
and "no VK configured yet" (bootstrap).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_read_state():
    from core.llm.cost import budget

    budget._last_read_reason = None
    yield
    budget._last_read_reason = None


# ---------------------------------------------------------------------------
# should_enforce — the gating function
# ---------------------------------------------------------------------------


def test_should_enforce_false_when_dev_mode_on(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "true")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")
    with patch(
        "core.llm.cost.budget._get_settings", return_value={"default_vk": "sk-bf-x"}
    ):
        from core.llm.cost.budget import should_enforce

        assert should_enforce() is False


def test_should_enforce_false_when_unlimited_env_on(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "true")
    with patch(
        "core.llm.cost.budget._get_settings", return_value={"default_vk": "sk-bf-x"}
    ):
        from core.llm.cost.budget import should_enforce

        assert should_enforce() is False


def test_should_enforce_false_when_no_vk_configured(monkeypatch):
    """Bootstrap window: no VK set → don't try to enforce. The dispatch
    omits the x-bf-vk header and Bifrost's no-VK path applies."""
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")
    with patch("core.llm.cost.budget._get_settings", return_value={"default_vk": ""}):
        from core.llm.cost.budget import should_enforce

        assert should_enforce() is False


def test_should_enforce_true_when_vk_set_and_no_bypass(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")
    with patch(
        "core.llm.cost.budget._get_settings",
        return_value={"default_vk": "sk-bf-real-key"},
    ):
        from core.llm.cost.budget import should_enforce

        assert should_enforce() is True


def test_get_active_vk_strips_whitespace():
    """Operator pastes a VK with surrounding whitespace from a config file
    — strip it so the header doesn't get mangled."""
    with patch(
        "core.llm.cost.budget._get_settings",
        return_value={"default_vk": "  sk-bf-padded   "},
    ):
        from core.llm.cost.budget import get_active_vk

        assert get_active_vk() == "sk-bf-padded"


def test_get_active_vk_returns_none_when_db_unavailable():
    """A misconfigured persistence layer must not block LLM traffic.
    The internal _get_settings catches DB errors and returns None;
    get_active_vk passes that through to the dispatch path which then
    falls back to bootstrap (no-VK) mode."""
    with patch(
        "core.llm.cost.budget._get_settings",
        return_value=None,
    ):
        from core.llm.cost.budget import get_active_vk

        assert get_active_vk() is None


def test_internal_get_settings_raises_and_public_readers_do_not():
    """_get_settings surfaces a DB failure so a read error is distinguishable
    from "no row"; nothing raises into the LLM path or the Budgets UI."""
    with patch(
        "core.llm.cost.budget.get_session", side_effect=RuntimeError("DB exploded")
    ):
        from core.llm.cost.budget import (
            _get_settings,
            get_active_vk,
            get_settings,
            should_enforce,
        )

        with pytest.raises(RuntimeError):
            _get_settings()
        assert get_active_vk() is None
        assert should_enforce() is False
        assert get_settings()["default_vk"] == ""


# ---------------------------------------------------------------------------
# get_settings / set_settings — config persistence
# ---------------------------------------------------------------------------


def test_get_settings_returns_normalized_defaults():
    """Empty / missing settings should normalize to safe defaults."""
    with patch("core.llm.cost.budget._get_settings", return_value=None):
        from core.llm.cost.budget import get_settings

        s = get_settings()
        assert s == {
            "default_vk": "",
            "budget_limit_usd": 0.0,
            "enforcement_mode": "warning",
        }


def test_set_settings_validates_enforcement_mode():
    from core.llm.cost.budget import set_settings

    with pytest.raises(ValueError, match="enforcement_mode must be"):
        set_settings(
            default_vk="sk-bf-x",
            budget_limit_usd=10.0,
            enforcement_mode="bogus",
            updated_by="tester",
        )


def test_stored_cap_and_mode_do_not_change_vk_header(monkeypatch):
    """The stored cap and mode are ignored. x-bf-vk follows default_vk only."""
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")
    from core.llm.router.router import bifrost_headers

    def attached(stored: dict) -> str | None:
        with patch("core.llm.cost.budget._get_settings", return_value=stored):
            return bifrost_headers().get("x-bf-vk")

    vk = "sk-bf-real-key"
    with_key = [
        attached(
            {"default_vk": vk, "enforcement_mode": mode, "budget_limit_usd": limit}
        )
        for mode in ("warning", "hard_stop")
        for limit in (0.0, 500.0)
    ]
    assert with_key == [vk, vk, vk, vk]

    without_key = [
        attached(
            {"default_vk": "", "enforcement_mode": mode, "budget_limit_usd": limit}
        )
        for mode in ("warning", "hard_stop")
        for limit in (0.0, 500.0)
    ]
    assert without_key == [None, None, None, None]


# ---------------------------------------------------------------------------
# BudgetExceeded — typed exception
# ---------------------------------------------------------------------------


def test_budget_exceeded_carries_tier_and_status():
    from core.llm.cost.budget import BudgetExceeded

    err = BudgetExceeded(
        tier="virtual_key", message="$10 spent of $10", status_code=402
    )
    assert err.tier == "virtual_key"
    assert err.status_code == 402
    assert "10 spent" in str(err)


# ---------------------------------------------------------------------------
# enforcement_status — reasons, transition logging, counter (#1567)
# ---------------------------------------------------------------------------

VK = "sk-bf-real-key"


@pytest.fixture
def no_bypass(monkeypatch):
    monkeypatch.setenv("DEV_MODE", "false")
    monkeypatch.setenv("LLM_BUDGET_UNLIMITED", "false")


@pytest.fixture
def unenforced():
    """Reasons passed to record_budget_unenforced by bifrost_headers."""
    with patch("core.llm.router.router.record_budget_unenforced") as rec:
        yield rec


def _headers():
    from core.llm.router.router import bifrost_headers

    return bifrost_headers()


def _budget_records(caplog):
    return [r for r in caplog.records if r.name == "core.llm.cost.budget"]


@pytest.mark.parametrize(
    ("env", "reason"), [("DEV_MODE", "dev_mode"), ("LLM_BUDGET_UNLIMITED", "unlimited")]
)
def test_bypass_reasons(no_bypass, monkeypatch, env, reason):
    from core.llm.cost.budget import enforcement_status

    monkeypatch.setenv(env, "true")
    with patch("core.llm.cost.budget._get_settings", return_value={"default_vk": VK}):
        assert enforcement_status() == (reason, None)


def test_enforced_and_not_configured_reasons(no_bypass):
    from core.llm.cost.budget import enforcement_status

    with patch("core.llm.cost.budget._get_settings", return_value={"default_vk": VK}):
        assert enforcement_status() == ("enforced", VK)
    for stored in (None, {"default_vk": ""}):
        with patch("core.llm.cost.budget._get_settings", return_value=stored):
            assert enforcement_status() == ("not_configured", None)


def test_read_error_warns_once_and_recovery_logs_once(no_bypass, caplog, unenforced):
    caplog.set_level(logging.INFO, logger="core.llm.cost.budget")
    with patch("core.llm.cost.budget._get_settings", return_value={"default_vk": VK}):
        assert _headers()["x-bf-vk"] == VK

    with patch("core.llm.cost.budget._get_settings", side_effect=OSError("db down")):
        outage = [_headers() for _ in range(5)]
    assert all("x-bf-vk" not in h for h in outage)
    warnings = [r for r in _budget_records(caplog) if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "without x-bf-vk" in warnings[0].getMessage()
    assert "db down" in warnings[0].getMessage()
    assert [c.args for c in unenforced.call_args_list] == [("read_error",)] * 5

    with patch("core.llm.cost.budget._get_settings", return_value={"default_vk": VK}):
        assert _headers()["x-bf-vk"] == VK
        assert _headers()["x-bf-vk"] == VK
    resumed = [r for r in _budget_records(caplog) if "resumed" in r.getMessage()]
    assert len(resumed) == 1 and resumed[0].levelno == logging.INFO


def test_read_error_with_no_prior_key_is_still_read_error(no_bypass, caplog):
    from core.llm.cost.budget import enforcement_status

    with patch("core.llm.cost.budget._get_settings", side_effect=OSError("db down")):
        assert enforcement_status() == ("read_error", None)
    assert [r.levelno for r in _budget_records(caplog)] == [logging.WARNING]


def test_not_configured_does_not_warn_but_is_counted(no_bypass, caplog, unenforced):
    caplog.set_level(logging.DEBUG, logger="core.llm.cost.budget")
    with patch("core.llm.cost.budget._get_settings", return_value=None):
        assert "x-bf-vk" not in _headers()
    assert not [r for r in _budget_records(caplog) if r.levelno >= logging.INFO]
    unenforced.assert_called_once_with("not_configured")


def test_bypass_is_counted_with_its_reason(no_bypass, monkeypatch, unenforced):
    monkeypatch.setenv("DEV_MODE", "true")
    with patch("core.llm.cost.budget._get_settings", return_value={"default_vk": VK}):
        assert "x-bf-vk" not in _headers()
    unenforced.assert_called_once_with("dev_mode")


def test_enforced_dispatch_is_not_counted(no_bypass, unenforced):
    with patch("core.llm.cost.budget._get_settings", return_value={"default_vk": VK}):
        assert _headers()["x-bf-vk"] == VK
    unenforced.assert_not_called()


def test_bifrost_headers_failure_warns_and_counts(caplog, unenforced):
    with patch(
        "core.llm.cost.budget.enforcement_status", side_effect=RuntimeError("boom")
    ):
        assert "x-bf-vk" not in _headers()
    assert [r.levelno for r in caplog.records if "unenforced" in r.getMessage()] == [
        logging.WARNING
    ]
    unenforced.assert_called_once_with("budget_unavailable")
