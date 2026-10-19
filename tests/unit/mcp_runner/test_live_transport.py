"""The live connector matrix records how each probe reached its server (#2308).

A run meant for the MCP runner must not pass on a stdio child, and the matrix
must say which transport each connector ran over.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.live import matrix
from tests.live.harness import Probe, check


class _Client:
    def __init__(self, runner):
        self.persistent_sessions = {"github": SimpleNamespace(runner=runner)}
        self.tools_cache = {"github": [{"name": "get_me"}]}

    async def connect_to_server(self, name, skip_enabled_check=False):
        return True

    def get_missing_credentials(self, name):
        return []

    def get_last_error(self, name):
        return None

    async def call_tool(self, name, tool, args, timeout=None):
        return {"content": [{"type": "text", "text": '{"login": "x"}'}]}


def _request():
    return SimpleNamespace(node=SimpleNamespace())


async def test_runner_session_is_recorded_as_runner(monkeypatch):
    monkeypatch.setenv("MCP_RUNNER_URL", "http://127.0.0.1:6991")
    request = _request()
    await check(Probe("github", "get_me"), _Client(runner=object()), request)
    assert request.node._live_transport == "runner"


async def test_stdio_child_fails_when_the_runner_is_expected(monkeypatch):
    monkeypatch.setenv("MCP_RUNNER_URL", "http://127.0.0.1:6991")
    request = _request()
    with pytest.raises(pytest.fail.Exception, match="ran as a stdio child"):
        await check(Probe("github", "get_me"), _Client(runner=None), request)
    assert request.node._live_transport == "stdio"


async def test_stdio_child_passes_without_the_runner(monkeypatch):
    monkeypatch.delenv("MCP_RUNNER_URL", raising=False)
    request = _request()
    await check(Probe("github", "get_me"), _Client(runner=None), request)
    assert request.node._live_transport == "stdio"


def test_matrix_shows_each_connectors_transport():
    def result(server, transport):
        return {
            "server": server,
            "probe": server,
            "tool": "t",
            "status": "pass",
            "detail": "ok",
            "version": "v",
            "transport": transport,
        }

    report = {
        "commit": "abc",
        "ran_at": "now",
        "results": [result("github", "runner"), result("gcp-scc", "stdio")],
    }
    lines = matrix.markdown(matrix.merge([report])).splitlines()
    assert "| Transport |" in lines[2]
    assert any(line.startswith("| github | v | ✅ pass | runner |") for line in lines)
    assert any(line.startswith("| gcp-scc | v | ✅ pass | stdio |") for line in lines)
