"""Unit tests for the MCP required-env-var extractor + credential gate.

These cover the dormancy-by-design path introduced with the #125 close:
when a server's ``mcp-config.json`` entry declares ``${VAR}`` placeholders
and those vars aren't set, we short-circuit before spawning the MCP
child and record the missing var names so the UI can show "Not
Configured" with specifics.
"""

from __future__ import annotations

import pytest
from unittest.mock import patch

from services.mcp_service import MCPServer, extract_required_env_vars


class TestExtractRequiredEnvVars:
    def test_scans_env_values(self):
        assert extract_required_env_vars(
            {"API_KEY": "${VIRUSTOTAL_API_KEY}"}, []
        ) == ["VIRUSTOTAL_API_KEY"]

    def test_scans_args(self):
        assert extract_required_env_vars(
            {}, ["-y", "mcp-remote", "${SPLUNK_MCP_URL}"]
        ) == ["SPLUNK_MCP_URL"]

    def test_scans_docker_style_args(self):
        # okta's config: `-e OKTA_API_TOKEN=${OKTA_API_TOKEN}` inside an arg.
        args = [
            "run",
            "-i",
            "--rm",
            "-e",
            "OKTA_DOMAIN=${OKTA_DOMAIN}",
            "-e",
            "OKTA_API_TOKEN=${OKTA_API_TOKEN}",
            "mcp/okta-mcp-fctr",
        ]
        result = extract_required_env_vars({}, args)
        assert result == ["OKTA_API_TOKEN", "OKTA_DOMAIN"]

    def test_deduplicates_across_env_and_args(self):
        result = extract_required_env_vars(
            {"URL": "${SPLUNK_MCP_URL}"}, ["-y", "mcp-remote", "${SPLUNK_MCP_URL}"]
        )
        assert result == ["SPLUNK_MCP_URL"]

    def test_ignores_workspaceFolder(self):
        # Not all ${...} references are credentials — path sentinels shouldn't be flagged.
        assert extract_required_env_vars(
            {"CWD": "${workspaceFolder}/data"}, []
        ) == []

    def test_returns_empty_when_no_placeholders(self):
        assert extract_required_env_vars(
            {"LITERAL_VALUE": "just-a-string"}, ["--flag", "value"]
        ) == []

    def test_returns_empty_on_none_inputs(self):
        assert extract_required_env_vars({}, []) == []
        assert extract_required_env_vars(None, None) == []  # type: ignore[arg-type]


class TestMCPServerRequiredEnvVars:
    def test_attribute_set_from_kwarg(self):
        server = MCPServer(
            name="test",
            command="python",
            args=[],
            cwd=".",
            env={},
            required_env_vars=["FOO", "BAR"],
        )
        assert server.required_env_vars == ["FOO", "BAR"]

    def test_default_is_empty_list(self):
        server = MCPServer(
            name="test",
            command="python",
            args=[],
            cwd=".",
            env={},
        )
        assert server.required_env_vars == []


class TestCredentialGate:
    """The short-circuit in MCPClient._missing_credentials_for()."""

    def _build_client(self):
        # Minimal stub: only _missing_credentials_for is exercised here,
        # so we instantiate MCPClient with a MagicMock-y service.
        from services.mcp_client import MCPClient

        class _StubService:
            servers: dict = {}

        return MCPClient(_StubService())

    def test_returns_missing_var_names(self):
        client = self._build_client()
        server = MCPServer(
            name="virustotal",
            command="npx",
            args=[],
            cwd=".",
            env={},
            required_env_vars=["VIRUSTOTAL_API_KEY"],
        )
        # Ensure env var + secrets manager both return falsy.
        with patch("os.environ.get", return_value=None), patch(
            "backend.secrets_manager.get_secret", return_value=None
        ):
            assert client._missing_credentials_for(server) == [
                "VIRUSTOTAL_API_KEY"
            ]

    def test_secrets_manager_satisfies_requirement(self):
        # A user who saved a credential via the integration wizard (which
        # writes to the encrypted store, not os.environ) should not be
        # told the server is dormant.
        client = self._build_client()
        server = MCPServer(
            name="virustotal",
            command="npx",
            args=[],
            cwd=".",
            env={},
            required_env_vars=["VIRUSTOTAL_API_KEY"],
        )
        with patch("os.environ.get", return_value=None), patch(
            "backend.secrets_manager.get_secret", return_value="sk-ant-xyz"
        ):
            assert client._missing_credentials_for(server) == []

    def test_no_required_env_vars_means_never_dormant(self):
        client = self._build_client()
        server = MCPServer(
            name="builtin",
            command="python",
            args=[],
            cwd=".",
            env={},
        )
        assert client._missing_credentials_for(server) == []


class TestSubstituteEnvVars:
    """_substitute_env_vars must handle plain ${VAR} and bash-style ${VAR:-default}."""

    def test_plain_substitution_when_set(self, monkeypatch):
        from services.mcp_service import MCPService

        service = MCPService()
        monkeypatch.setenv("MY_VAR", "hello")
        assert service._substitute_env_vars("${MY_VAR}") == "hello"

    def test_plain_substitution_when_unset(self):
        from services.mcp_service import MCPService

        service = MCPService()
        assert service._substitute_env_vars("${UNSET_VAR_XYZ}") == ""

    def test_default_value_when_unset(self):
        from services.mcp_service import MCPService

        service = MCPService()
        assert service._substitute_env_vars("${UNSET_VAR_XYZ:-default}") == "default"

    def test_default_value_ignored_when_set(self, monkeypatch):
        from services.mcp_service import MCPService

        service = MCPService()
        monkeypatch.setenv("MY_VAR", "override")
        assert service._substitute_env_vars("${MY_VAR:-default}") == "override"

    def test_nested_substitution_in_default(self, monkeypatch):
        from services.mcp_service import MCPService

        service = MCPService()
        monkeypatch.setenv("HOME", "/Users/test")
        assert (
            service._substitute_env_vars("${UNSET_VAR_XYZ:-${HOME}/.vigil/palace}")
            == "/Users/test/.vigil/palace"
        )

    def test_nested_default_when_both_unset(self):
        from services.mcp_service import MCPService

        service = MCPService()
        assert (
            service._substitute_env_vars("${A:-${B:-fallback}}") == "fallback"
        )

    def test_mempalace_config_path(self, monkeypatch):
        """Real-world shape from mcp-config.json."""
        from services.mcp_service import MCPService

        service = MCPService()
        monkeypatch.setenv("HOME", "/Users/test")
        result = service._substitute_env_vars(
            "${MEMPALACE_PALACE_PATH:-${HOME}/.vigil/mempalace/palace}"
        )
        assert result == "/Users/test/.vigil/mempalace/palace"

        # When the env var is explicitly set, it wins.
        monkeypatch.setenv("MEMPALACE_PALACE_PATH", "/custom/palace")
        result = service._substitute_env_vars(
            "${MEMPALACE_PALACE_PATH:-${HOME}/.vigil/mempalace/palace}"
        )
        assert result == "/custom/palace"


class TestRetryDormantIfReady:
    """`retry_dormant_if_ready` should only retry servers whose creds now resolve."""

    def _build_client_with_server(self, server: MCPServer):
        from services.mcp_client import MCPClient

        class _StubService:
            def __init__(self, srv):
                self.servers = {srv.name: srv}

        return MCPClient(_StubService(server))

    @pytest.mark.asyncio
    async def test_noop_when_nothing_dormant(self):
        server = MCPServer(
            name="builtin",
            command="python",
            args=[],
            cwd=".",
            env={},
        )
        client = self._build_client_with_server(server)
        # Nothing in last_missing_credentials → empty result, no retries.
        result = await client.retry_dormant_if_ready()
        assert result == {}

    @pytest.mark.asyncio
    async def test_skips_server_with_still_missing_creds(self):
        server = MCPServer(
            name="virustotal",
            command="npx",
            args=[],
            cwd=".",
            env={},
            required_env_vars=["VIRUSTOTAL_API_KEY"],
        )
        client = self._build_client_with_server(server)
        # Mark as dormant.
        client.last_missing_credentials = {"virustotal": ["VIRUSTOTAL_API_KEY"]}
        # Env and secrets both still empty.
        with patch("os.environ.get", return_value=None), patch(
            "backend.secrets_manager.get_secret", return_value=None
        ):
            result = await client.retry_dormant_if_ready()
        assert result == {}
        # Nothing was retried, so last_retry_at stays empty.
        assert client._last_retry_at == {}

    @pytest.mark.asyncio
    async def test_retries_when_cred_becomes_available(self):
        from unittest.mock import AsyncMock

        server = MCPServer(
            name="virustotal",
            command="npx",
            args=[],
            cwd=".",
            env={},
            required_env_vars=["VIRUSTOTAL_API_KEY"],
        )
        client = self._build_client_with_server(server)
        client.last_missing_credentials = {"virustotal": ["VIRUSTOTAL_API_KEY"]}
        # Stub connect_to_server to record the call + simulate success.
        client.connect_to_server = AsyncMock(return_value=True)
        # Secrets manager now returns a key — creds resolve.
        with patch("os.environ.get", return_value=None), patch(
            "backend.secrets_manager.get_secret", return_value="vt-key-xyz"
        ):
            result = await client.retry_dormant_if_ready()
        assert result == {"virustotal": True}
        client.connect_to_server.assert_awaited_once_with(
            "virustotal", persistent=True
        )

    @pytest.mark.asyncio
    async def test_rate_limits_repeated_retries(self):
        from unittest.mock import AsyncMock

        server = MCPServer(
            name="virustotal",
            command="npx",
            args=[],
            cwd=".",
            env={},
            required_env_vars=["VIRUSTOTAL_API_KEY"],
        )
        client = self._build_client_with_server(server)
        client.last_missing_credentials = {"virustotal": ["VIRUSTOTAL_API_KEY"]}
        client.connect_to_server = AsyncMock(return_value=False)
        with patch("os.environ.get", return_value=None), patch(
            "backend.secrets_manager.get_secret", return_value="vt-key-xyz"
        ):
            # First call: attempts retry.
            r1 = await client.retry_dormant_if_ready()
            # Second call, immediately after: rate-limited.
            r2 = await client.retry_dormant_if_ready()
        assert "virustotal" in r1
        assert r2 == {}
        # connect_to_server was called exactly once despite two sweeps.
        assert client.connect_to_server.await_count == 1
