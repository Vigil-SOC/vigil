"""The egress proxy reaching a spawned MCP tool server.

stdio_client narrows the child environment, so a proxy the runner holds never
reaches its servers unless forwarded; Node's fetch also needs
NODE_USE_ENV_PROXY before it reads the variables at all.
"""

from __future__ import annotations

import json

import pytest

from core.integrations.mcp.child_env import PROXY_ENV_NAMES, proxy_env
from services.mcp_runner.catalog import Catalog

pytestmark = pytest.mark.unit

PROXY = "http://proxy.corp:3128"


@pytest.fixture(autouse=True)
def _clear_proxy_vars(monkeypatch):
    for var in (*PROXY_ENV_NAMES, "NODE_USE_ENV_PROXY"):
        monkeypatch.delenv(var, raising=False)


def test_no_proxy_configured_leaves_the_child_env_unchanged(monkeypatch):
    assert proxy_env() == {}
    # A bypass list alone routes nothing, so it does not set the flag either.
    monkeypatch.setenv("NO_PROXY", "localhost")
    assert proxy_env() == {}


def test_both_spellings_forward_with_the_node_flag(monkeypatch):
    monkeypatch.setenv("https_proxy", PROXY)
    monkeypatch.setenv("HTTP_PROXY", PROXY)
    monkeypatch.setenv("NO_PROXY", "localhost,.internal")
    monkeypatch.setenv("ALL_PROXY", "")

    assert proxy_env() == {
        "https_proxy": PROXY,
        "HTTP_PROXY": PROXY,
        "NO_PROXY": "localhost,.internal",
        "NODE_USE_ENV_PROXY": "1",
    }


def _catalog(root, env):
    (root / "mcp-config.json").write_text(
        json.dumps({"mcpServers": {"node": {"command": "node", "env": env}}})
    )
    return Catalog(root)


def test_the_entry_env_wins_over_the_forwarded_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTPS_PROXY", PROXY)
    monkeypatch.setenv("no_proxy", "localhost")
    catalog = _catalog(
        tmp_path, {"NO_PROXY": "misp.internal", "NODE_USE_ENV_PROXY": "0"}
    )

    env = catalog.launch("node", {}, "/h").env
    assert env["HTTPS_PROXY"] == PROXY
    assert env["NO_PROXY"] == "misp.internal"
    # The other spelling would disagree with the entry's bypass list.
    assert "no_proxy" not in env
    assert env["NODE_USE_ENV_PROXY"] == "0"
    # And the backend's request env wins over both.
    env = catalog.launch("node", {"https_proxy": "http://other:8080"}, "/h").env
    assert env["https_proxy"] == "http://other:8080"
    assert "HTTPS_PROXY" not in env


def test_launch_without_a_proxy_adds_nothing(tmp_path):
    env = _catalog(tmp_path, {}).launch("node", {}, "/h").env
    assert not {*PROXY_ENV_NAMES, "NODE_USE_ENV_PROXY"} & set(env)
