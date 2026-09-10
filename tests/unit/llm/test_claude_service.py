"""Unit tests for ClaudeService one-shot construction and key loading."""

from unittest.mock import patch

from core.llm.harness.claude import ClaudeService


class TestClaudeServiceInitialization:
    @patch("core.llm.harness.claude.get_secret")
    def test_init_default_config(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        service = ClaudeService()

        assert service.api_key == "test-api-key-123"
        assert service.client is not None

    @patch("core.llm.router.router.discover_anthropic_api_key", return_value=None)
    @patch("core.llm.harness.claude.get_secret")
    def test_init_no_api_key(self, mock_get_secret, _discover):
        mock_get_secret.return_value = None

        service = ClaudeService()

        assert service.api_key is None
        assert service.client is None
        assert service.async_client is None

    @patch("core.llm.router.router.discover_anthropic_api_key")
    @patch("core.llm.harness.claude.get_secret")
    def test_init_discovers_ui_saved_key(self, mock_get_secret, mock_discover):
        """Issue #292: when neither CLAUDE_API_KEY nor ANTHROPIC_API_KEY
        is set in secrets/env, ClaudeService falls back to looking up an
        Anthropic provider row in llm_provider_configs (the UI-saved
        path) and reads its api_key_ref from the secrets manager.
        """
        mock_get_secret.return_value = None
        mock_discover.return_value = "sk-ant-ui-saved-key"

        service = ClaudeService()

        assert service.api_key == "sk-ant-ui-saved-key"
        assert mock_discover.call_count == 1

    @patch("core.llm.router.router.discover_anthropic_api_key")
    @patch("core.llm.harness.claude.get_secret")
    def test_init_does_not_call_discovery_when_legacy_key_present(
        self, mock_get_secret, mock_discover
    ):
        mock_get_secret.return_value = "sk-ant-legacy-env-key"

        service = ClaudeService()

        assert service.api_key == "sk-ant-legacy-env-key"
        mock_discover.assert_not_called()
