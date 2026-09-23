"""Unit tests for core.platform.runtime_config (GH #84 PR-F).

Covers the DB → env → default resolution order and the in-process cache.
The surviving keys are the local-Ollama recovery toggles.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit

_REMOVED = (
    "history_window",
    "thinking_budget",
    "prompt_cache_enabled",
    "tool_response_budget_default",
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with a clean runtime-config cache."""
    from core.platform import runtime_config

    runtime_config.clear_cache()
    yield
    runtime_config.clear_cache()


class TestResolutionOrder:
    def test_db_value_wins_over_env(self, monkeypatch):
        from core.platform import runtime_config

        monkeypatch.setenv("LOCAL_OLLAMA_RECOVERY_RETRY_LIMIT", "1")
        with patch.object(
            runtime_config,
            "_fetch_db_config",
            return_value={"local_ollama_recovery_retry_limit": 3},
        ):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_retry_limit", 1
                )
                == 3
            )

    def test_env_wins_when_db_missing(self, monkeypatch):
        from core.platform import runtime_config

        monkeypatch.setenv("LOCAL_OLLAMA_RECOVERY_RETRY_LIMIT", "2")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_retry_limit", 1
                )
                == 2
            )

    def test_default_when_nothing_set(self, monkeypatch):
        from core.platform import runtime_config

        monkeypatch.delenv("LOCAL_OLLAMA_RECOVERY_RETRY_LIMIT", raising=False)
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_retry_limit", 1
                )
                == 1
            )

    def test_db_fetch_failure_falls_through_to_env(self, monkeypatch):
        from core.platform import runtime_config

        monkeypatch.setenv("LOCAL_OLLAMA_RECOVERY_RETRY_LIMIT", "2")
        # _fetch_db_config returning None (our convention for "DB unavailable")
        # still lets env/default win.
        with patch.object(runtime_config, "_fetch_db_config", return_value=None):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_retry_limit", 1
                )
                == 2
            )

    def test_unknown_key_skips_env_lookup(self, monkeypatch):
        """Keys not in ENV_FALLBACKS go straight to default — no risk of a
        typo silently reading an unrelated env var."""
        from core.platform import runtime_config

        monkeypatch.setenv("NONSENSE_KEY", "99")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert runtime_config.get_ai_operations_setting("nonsense_key", 5) == 5

    def test_stale_keys_do_not_block_a_live_read(self):
        """An existing row may still hold the removed cost/perf keys."""
        from core.platform import runtime_config

        blob = {key: 1 for key in _REMOVED}
        blob["local_ollama_recovery_retry_limit"] = 2
        with patch.object(runtime_config, "_fetch_db_config", return_value=blob):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_retry_limit", 1
                )
                == 2
            )


class TestTypeCoercion:
    def test_bool_from_string(self, monkeypatch):
        from core.platform import runtime_config

        monkeypatch.setenv("LOCAL_OLLAMA_RECOVERY_ENABLED", "false")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_enabled", True
                )
                is False
            )

    def test_bool_preserved_from_db(self):
        from core.platform import runtime_config

        with patch.object(
            runtime_config,
            "_fetch_db_config",
            return_value={"local_ollama_recovery_enabled": False},
        ):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_enabled", True
                )
                is False
            )

    def test_int_coerced_from_env_string(self, monkeypatch):
        from core.platform import runtime_config

        monkeypatch.setenv("LOCAL_OLLAMA_RECOVERY_RETRY_LIMIT", "2")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_retry_limit", 1
                )
                == 2
            )

    def test_bad_int_falls_back_to_default(self, monkeypatch):
        from core.platform import runtime_config

        monkeypatch.setenv("LOCAL_OLLAMA_RECOVERY_RETRY_LIMIT", "not-a-number")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_retry_limit", 1
                )
                == 1
            )

    def test_local_recovery_restart_bool_env_fallback(self, monkeypatch):
        from core.platform import runtime_config

        monkeypatch.setenv("LOCAL_OLLAMA_RECOVERY_RESTART_GATEWAY", "false")
        with patch.object(runtime_config, "_fetch_db_config", return_value={}):
            assert (
                runtime_config.get_ai_operations_setting(
                    "local_ollama_recovery_restart_gateway", True
                )
                is False
            )


class TestAIOperationsSettingsModel:
    """The Settings-UI config model exposes only the local-recovery toggles."""

    def test_defaults_include_local_recovery_toggles(self):
        from services.api.routers.config import AI_OPERATIONS_DEFAULTS

        assert set(AI_OPERATIONS_DEFAULTS) == {
            "local_ollama_recovery_enabled",
            "local_ollama_recovery_retry_limit",
            "local_ollama_recovery_restart_gateway",
        }
        assert AI_OPERATIONS_DEFAULTS["local_ollama_recovery_enabled"] is True
        assert AI_OPERATIONS_DEFAULTS["local_ollama_recovery_retry_limit"] == 1
        assert AI_OPERATIONS_DEFAULTS["local_ollama_recovery_restart_gateway"] is True

    def test_removed_keys_are_ignored_on_write(self):
        from services.api.routers.config import AIOperationsSettingsConfig

        cfg = AIOperationsSettingsConfig.model_validate(
            {
                "local_ollama_recovery_retry_limit": 2,
                "history_window": 5,
                "thinking_budget": 1,
                "prompt_cache_enabled": False,
                "tool_response_budget_default": 100,
            }
        )
        dumped = cfg.model_dump()
        assert dumped["local_ollama_recovery_retry_limit"] == 2
        assert set(dumped).isdisjoint(_REMOVED)

    def test_retry_limit_rejects_out_of_range(self):
        from pydantic import ValidationError

        from services.api.routers.config import AIOperationsSettingsConfig

        with pytest.raises(ValidationError):
            AIOperationsSettingsConfig(local_ollama_recovery_retry_limit=5)
        with pytest.raises(ValidationError):
            AIOperationsSettingsConfig(local_ollama_recovery_retry_limit=-1)

    @pytest.mark.asyncio
    async def test_get_omits_stale_stored_keys(self):
        from services.api.routers.config import (
            AI_OPERATIONS_DEFAULTS,
            get_ai_operations_config,
        )

        svc = MagicMock()
        svc.get_system_config.return_value = {
            "local_ollama_recovery_enabled": False,
            "local_ollama_recovery_retry_limit": 3,
            "history_window": 5,
            "thinking_budget": 1,
            "prompt_cache_enabled": False,
            "tool_response_budget_default": 100,
        }
        with patch("services.api.routers.config.get_config_service", return_value=svc):
            body = await get_ai_operations_config()
        assert body["local_ollama_recovery_enabled"] is False
        assert body["local_ollama_recovery_retry_limit"] == 3
        assert body["local_ollama_recovery_restart_gateway"] is True
        assert set(body) == set(AI_OPERATIONS_DEFAULTS)
        assert set(body).isdisjoint(_REMOVED)


class TestCacheBehavior:
    def test_cache_avoids_repeated_db_fetch(self):
        from core.platform import runtime_config

        fetch_mock = patch.object(
            runtime_config,
            "_fetch_db_config",
            return_value={"local_ollama_recovery_retry_limit": 2},
        )
        with fetch_mock as m:
            runtime_config.get_ai_operations_setting(
                "local_ollama_recovery_retry_limit", 1
            )
            runtime_config.get_ai_operations_setting(
                "local_ollama_recovery_retry_limit", 1
            )
            runtime_config.get_ai_operations_setting(
                "local_ollama_recovery_enabled", True
            )
            # Three reads, one DB fetch (cache holds the dict).
            assert m.call_count == 1

    def test_clear_cache_triggers_refetch(self):
        from core.platform import runtime_config

        with patch.object(
            runtime_config,
            "_fetch_db_config",
            return_value={"local_ollama_recovery_retry_limit": 3},
        ) as m:
            runtime_config.get_ai_operations_setting(
                "local_ollama_recovery_retry_limit", 1
            )
            runtime_config.clear_cache()
            runtime_config.get_ai_operations_setting(
                "local_ollama_recovery_retry_limit", 1
            )
            assert m.call_count == 2
