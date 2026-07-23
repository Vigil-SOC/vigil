"""Unit tests for services.runtime_config (GH #84 PR-F).

Covers the DB → env → default resolution order and the in-process cache
behavior used by ClaudeService / AgentRunner consumers.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with a clean runtime-config cache."""
    from services import runtime_config

    runtime_config.clear_cache()
    yield
    runtime_config.clear_cache()


class TestResolutionOrder:
    def test_db_value_wins_over_env(self, monkeypatch):
        from services import runtime_config

        monkeypatch.setenv("CLAUDE_HISTORY_WINDOW", "5")
        with patch.object(
            runtime_config, "_fetch_db_config", return_value={"history_window": 42}
        ):
            assert runtime_config.get_ai_operations_setting("history_window", 20) == 42

    def test_env_wins_when_db_missing(self, monkeypatch):
        from services import runtime_config

        monkeypatch.setenv("CLAUDE_HISTORY_WINDOW", "7")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert runtime_config.get_ai_operations_setting("history_window", 20) == 7

    def test_default_when_nothing_set(self, monkeypatch):
        from services import runtime_config

        monkeypatch.delenv("CLAUDE_HISTORY_WINDOW", raising=False)
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert runtime_config.get_ai_operations_setting("history_window", 20) == 20

    def test_db_fetch_failure_falls_through_to_env(self, monkeypatch):
        from services import runtime_config

        monkeypatch.setenv("CLAUDE_HISTORY_WINDOW", "11")
        # _fetch_db_config returning None (our convention for "DB unavailable")
        # still lets env/default win.
        with patch.object(runtime_config, "_fetch_db_config", return_value=None):
            assert runtime_config.get_ai_operations_setting("history_window", 20) == 11

    def test_unknown_key_skips_env_lookup(self, monkeypatch):
        """Keys not in ENV_FALLBACKS go straight to default — no risk of a
        typo silently reading an unrelated env var."""
        from services import runtime_config

        monkeypatch.setenv("NONSENSE_KEY", "99")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert runtime_config.get_ai_operations_setting("nonsense_key", 5) == 5


class TestTypeCoercion:
    def test_bool_from_string(self, monkeypatch):
        from services import runtime_config

        monkeypatch.setenv("ANTHROPIC_PROMPT_CACHE_ENABLED", "false")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting("prompt_cache_enabled", True)
                is False
            )

    def test_bool_preserved_from_db(self):
        from services import runtime_config

        with patch.object(
            runtime_config,
            "_fetch_db_config",
            return_value={"prompt_cache_enabled": False},
        ):
            assert (
                runtime_config.get_ai_operations_setting("prompt_cache_enabled", True)
                is False
            )

    def test_int_coerced_from_env_string(self, monkeypatch):
        from services import runtime_config

        monkeypatch.setenv("CLAUDE_THINKING_BUDGET", "4096")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting("thinking_budget", 10000)
                == 4096
            )

    def test_bad_int_falls_back_to_default(self, monkeypatch):
        from services import runtime_config

        monkeypatch.setenv("CLAUDE_THINKING_BUDGET", "not-a-number")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting("thinking_budget", 10000)
                == 10000
            )

    def test_local_recovery_uses_its_own_env_fallback(self, monkeypatch):
        from services import runtime_config

        monkeypatch.setenv("LOCAL_OLLAMA_RECOVERY_RETRY_LIMIT", "2")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_retry_limit", 1
                )
                == 2
            )

    def test_local_recovery_enabled_bool_env_fallback(self, monkeypatch):
        from services import runtime_config

        monkeypatch.setenv("LOCAL_OLLAMA_RECOVERY_ENABLED", "false")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_enabled", True
                )
                is False
            )

    def test_local_recovery_restart_bool_env_fallback(self, monkeypatch):
        from services import runtime_config

        monkeypatch.setenv("LOCAL_OLLAMA_RECOVERY_RESTART_GATEWAY", "false")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_restart_gateway", True
                )
                is False
            )


class TestAIOperationsSettingsModel:
    """The Settings-UI config model must expose the local-recovery toggles
    with safe defaults and clamp the retry limit to the documented range."""

    def test_defaults_include_local_recovery_toggles(self):
        from backend.api.config import AI_OPERATIONS_DEFAULTS

        assert AI_OPERATIONS_DEFAULTS["local_ollama_recovery_enabled"] is True
        assert AI_OPERATIONS_DEFAULTS["local_ollama_recovery_retry_limit"] == 1
        assert AI_OPERATIONS_DEFAULTS["local_ollama_recovery_restart_gateway"] is True

    def test_retry_limit_rejects_out_of_range(self):
        from pydantic import ValidationError

        from backend.api.config import AIOperationsSettingsConfig

        with pytest.raises(ValidationError):
            AIOperationsSettingsConfig(local_ollama_recovery_retry_limit=5)
        with pytest.raises(ValidationError):
            AIOperationsSettingsConfig(local_ollama_recovery_retry_limit=-1)


class TestCacheBehavior:
    def test_cache_avoids_repeated_db_fetch(self):
        from services import runtime_config

        fetch_mock = patch.object(
            runtime_config,
            "_fetch_db_config",
            return_value={"history_window": 15},
        )
        with fetch_mock as m:
            runtime_config.get_ai_operations_setting("history_window", 20)
            runtime_config.get_ai_operations_setting("history_window", 20)
            runtime_config.get_ai_operations_setting("thinking_budget", 10000)
            # Three reads, one DB fetch (cache holds the dict).
            assert m.call_count == 1

    def test_clear_cache_triggers_refetch(self):
        from services import runtime_config

        with patch.object(
            runtime_config, "_fetch_db_config", return_value={"history_window": 3}
        ) as m:
            runtime_config.get_ai_operations_setting("history_window", 20)
            runtime_config.clear_cache()
            runtime_config.get_ai_operations_setting("history_window", 20)
            assert m.call_count == 2


class TestConsumerIntegration:
    """End-to-end checks that the ClaudeService / AgentRunner helpers now
    honor the DB-backed settings, not just env vars.
    """

    def test_history_window_respects_db(self):
        from services import runtime_config
        from services.claude_service import ClaudeService

        with patch.object(
            runtime_config, "_fetch_db_config", return_value={"history_window": 3}
        ):
            msgs = [{"role": "user", "content": f"m{i}"} for i in range(10)]
            out = ClaudeService._apply_history_window(msgs)
        assert len(out) == 6  # 3 turns = 6 messages
        assert out[-1]["content"] == "m9"

    def test_thinking_budget_respects_db(self):
        from services import runtime_config
        from daemon.agent_runner import _default_thinking_budget

        with patch.object(
            runtime_config, "_fetch_db_config", return_value={"thinking_budget": 2048}
        ):
            assert _default_thinking_budget() == 2048

    def test_prompt_cache_kill_switch_from_db(self):
        from services import runtime_config
        from services.claude_service import ClaudeService

        with patch.object(
            runtime_config,
            "_fetch_db_config",
            return_value={"prompt_cache_enabled": False},
        ):
            kw = {"system": "big system prompt"}
            ClaudeService._apply_prompt_cache_controls(kw)
        # Kill switch engaged → system stays a bare string, no cache_control block.
        assert kw["system"] == "big system prompt"

    def test_tool_response_budget_from_db(self):
        from services import runtime_config
        from services.claude_service import ClaudeService

        with patch.object(
            runtime_config,
            "_fetch_db_config",
            return_value={"tool_response_budget_default": 1234},
        ):
            # unknown_tool falls through to default lookup
            assert ClaudeService._response_budget_for("unknown_tool") == 1234
