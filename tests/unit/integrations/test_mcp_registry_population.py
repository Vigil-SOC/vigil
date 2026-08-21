"""The MCP registry is populated explicitly, not as a side effect of an LLM client.

Before #632 the tool loader in ClaudeService filled this registry on its way past,
so ``workflow_ai_generator`` and ``agent_ai_generator`` silently depended on
somebody having constructed an LLM client first. These pin the replacement.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.integrations.mcp import registry as registry_module
from core.integrations.mcp.registry import MCPRegistry, populate_from_cache

pytestmark = pytest.mark.unit

# The registry prefixes a tool with its server, and callers rely on it.
CACHED = {
    "splunk": [
        {
            "name": "splunk_search",
            "description": "run a search",
            "inputSchema": {"type": "object"},
        }
    ],
    "shodan": [
        {
            "name": "shodan_host",
            "description": "look up a host",
            "inputSchema": {"type": "object"},
        }
    ],
}


@pytest.fixture()
def fresh_registry():
    return MCPRegistry()


def _client(connected):
    client = MagicMock()
    client.tools_cache = {}
    client.get_connection_status.return_value = connected
    client.mcp_service.servers = {}
    return client


def _populate(tmp_path, monkeypatch, registry, connected, cached=CACHED, eager=True):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "mcp_tools_cache.json").write_text(json.dumps(cached))
    monkeypatch.setattr(registry_module, "REPO_ROOT", tmp_path, raising=False)
    # Stated rather than inherited from the environment: whether live connection
    # state gates the cache depends on whether this boot dialled at all, and a test
    # that reads that from a developer's .env passes or fails by accident.
    monkeypatch.setattr(registry_module, "eager_connect_enabled", lambda: eager)

    with patch("core.config.REPO_ROOT", tmp_path):
        with patch(
            "core.integrations.mcp.client.process_mcp_client",
            return_value=_client(connected),
        ):
            return populate_from_cache(registry)


def test_registers_the_tools_of_a_connected_server(
    tmp_path, monkeypatch, fresh_registry
):
    assert (
        _populate(
            tmp_path, monkeypatch, fresh_registry, {"splunk": True, "shodan": True}
        )
        == 2
    )
    assert set(fresh_registry.get_tool_names()) == {
        "splunk_splunk_search",
        "shodan_shodan_host",
    }


def test_skips_a_server_cached_but_not_connected_this_boot(
    tmp_path, monkeypatch, fresh_registry
):
    # The cache is a warm-start artifact (#129). Registering a server that failed
    # to connect lets a model claim a capability it cannot exercise.
    assert (
        _populate(
            tmp_path, monkeypatch, fresh_registry, {"splunk": True, "shodan": False}
        )
        == 1
    )
    assert fresh_registry.get_tool_names() == ["splunk_splunk_search"]


def test_registers_a_cached_server_when_this_boot_dialled_nothing(
    tmp_path, monkeypatch, fresh_registry
):
    # With eager connect off, no server is connected until a call arrives and the
    # client reconnects for it. Gating on live state there drops the whole cache and
    # leaves every capability those servers answer unbound for the boot.
    assert (
        _populate(
            tmp_path,
            monkeypatch,
            fresh_registry,
            {"splunk": False, "shodan": False},
            eager=False,
        )
        == 2
    )
    assert set(fresh_registry.get_tool_names()) == {
        "splunk_splunk_search",
        "shodan_shodan_host",
    }


def test_registers_nothing_when_the_cache_is_empty(
    tmp_path, monkeypatch, fresh_registry
):
    assert _populate(tmp_path, monkeypatch, fresh_registry, {}, cached={}) == 0
    assert fresh_registry.get_tool_names() == []


def test_needs_no_llm_client(tmp_path, monkeypatch, fresh_registry):
    """The point of #632: constructing an LLM client is not how tools get discovered."""
    with patch.dict(sys.modules, {"core.llm.harness.claude": None}):
        assert (
            _populate(
                tmp_path, monkeypatch, fresh_registry, {"splunk": True, "shodan": True}
            )
            == 2
        )
    assert "splunk_splunk_search" in fresh_registry.get_tool_names()


def test_the_generators_read_what_startup_registered(
    tmp_path, monkeypatch, fresh_registry
):
    # The generators read the instance startup populated, which is the coupling
    # this makes explicit rather than incidental.
    _populate(tmp_path, monkeypatch, fresh_registry, {"splunk": True, "shodan": True})
    assert set(fresh_registry.get_tool_names()) == {
        "splunk_splunk_search",
        "shodan_shodan_host",
    }


# Ported from TestLoadMcpToolsCache in test_claude_service.py (#632). The loader
# moved off the LLM client; these four behaviours came with it.
def test_falls_back_to_the_in_memory_cache_when_no_file_exists(
    tmp_path, monkeypatch, fresh_registry
):
    client = _client({"threat_intel": True})
    client.tools_cache = {
        "threat_intel": [
            {
                "name": "lookup_ip",
                "description": "Lookup an IP",
                "inputSchema": {"type": "object"},
            }
        ]
    }
    monkeypatch.setattr(registry_module, "REPO_ROOT", tmp_path, raising=False)

    with patch("core.config.REPO_ROOT", tmp_path):
        with patch(
            "core.integrations.mcp.client.process_mcp_client", return_value=client
        ):
            assert populate_from_cache(fresh_registry) == 1
    assert fresh_registry.get_tool_names() == ["threat_intel_lookup_ip"]


def test_falls_back_to_memory_when_the_cache_file_is_malformed(
    tmp_path, monkeypatch, fresh_registry
):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "mcp_tools_cache.json").write_text("{not valid json}")
    client = _client({"threat_intel": True})
    client.tools_cache = {
        "threat_intel": [
            {
                "name": "lookup_ip",
                "description": "Lookup an IP",
                "inputSchema": {"type": "object"},
            }
        ]
    }

    with patch("core.config.REPO_ROOT", tmp_path):
        with patch(
            "core.integrations.mcp.client.process_mcp_client", return_value=client
        ):
            assert populate_from_cache(fresh_registry) == 1
    assert fresh_registry.get_tool_names() == ["threat_intel_lookup_ip"]


def test_creates_no_event_loop(tmp_path, monkeypatch, fresh_registry):
    # Reading a cache is synchronous. Spinning a loop here deadlocks a caller
    # that already has one running.
    with patch("asyncio.new_event_loop") as new_loop:
        _populate(
            tmp_path, monkeypatch, fresh_registry, {"splunk": True, "shodan": True}
        )
    new_loop.assert_not_called()


def test_keeps_the_input_schema_a_tool_declared(tmp_path, monkeypatch, fresh_registry):
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    cached = {
        "splunk": [
            {"name": "search", "description": "Search logs", "inputSchema": schema}
        ]
    }
    _populate(tmp_path, monkeypatch, fresh_registry, {"splunk": True}, cached=cached)

    registered = fresh_registry.get_all_tools()
    # get_all_tools emits input_schema, the shape a model is handed.
    assert registered[0]["input_schema"] == schema
