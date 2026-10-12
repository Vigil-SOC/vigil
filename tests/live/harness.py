"""The one way a live check reaches a connector.

Every probe goes through the production ``MCPService`` and ``MCPClient``: the
server is spawned from its ``mcp-config.json`` entry with the env Vigil would
give it, so a pass means the shipped launcher, pin, and config path work, not
just the vendor API.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pytest

from tests.live.conftest import REPO_ROOT


@dataclass(frozen=True)
class Probe:
    """One read-only call that only a working connector can answer.

    ``expect`` receives the tool's answer (JSON-decoded when it is JSON) and
    asserts on it; a string it returns goes into the matrix as the evidence.
    ``requires`` names env vars the probe cannot run without, for in-repo
    servers whose ``mcp-config.json`` entry declares no ``${VAR}`` gate.
    """

    server: str
    tool: str
    args: dict = field(default_factory=dict)
    expect: Callable[[Any], Optional[str]] = lambda payload: None
    requires: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    timeout: float = 60.0
    name: str = ""

    @property
    def id(self) -> str:
        return f"{self.server}:{self.name}" if self.name else self.server

    def __repr__(self) -> str:  # pytest ids
        return self.id


def build_client():
    from core.integrations.mcp.client import MCPClient
    from core.integrations.mcp.service import MCPService

    return MCPClient(MCPService(project_root=REPO_ROOT))


def connector_version(server: str) -> str:
    """The pinned upstream version from ``mcp-config.json``, or ``in-repo``."""
    entry = json.loads((REPO_ROOT / "mcp-config.json").read_text())["mcpServers"][server]
    args = entry.get("args", [])
    for i, arg in enumerate(args):
        if arg.startswith("-") or (i and args[i - 1] == "--with"):  # --with: a helper, not the connector
            continue
        name = arg.rsplit("/", 1)[-1]
        if "@sha256:" in name:  # an image digest: enough of it to tell two apart
            return name[: name.index("@sha256:") + 20]
        if re.search(r"(?:@|==)v?\d[\w.\-]*$", name):
            return name
    if entry.get("command") == "python3":
        return "in-repo"
    return entry.get("args", ["unknown"])[-1]


def _payload(result: dict) -> Any:
    texts = [c.get("text", "") for c in result.get("content") or []]
    text = "\n".join(t for t in texts if t)
    try:
        return json.loads(text)
    except ValueError:
        return text


async def check(probe: Probe, client, request) -> None:
    missing = [var for var in probe.requires if not os.environ.get(var)]
    if missing:
        pytest.skip(f"not configured: {', '.join(missing)}")

    connected = await client.connect_to_server(probe.server, skip_enabled_check=True)
    gated = client.get_missing_credentials(probe.server)
    if gated:
        pytest.skip(f"not configured: {', '.join(gated)}")
    assert connected, f"could not start {probe.server}: {client.get_last_error(probe.server)}"

    listed = {tool["name"] for tool in client.tools_cache.get(probe.server, [])}
    absent = sorted(set(probe.tools or (probe.tool,)) - listed)
    assert not absent, f"{probe.server} no longer lists {absent}"

    result = await client.call_tool(probe.server, probe.tool, probe.args, timeout=probe.timeout)
    payload = _payload(result)
    assert not result.get("error"), f"{probe.tool} returned an error: {str(payload)[:300]}"
    evidence = probe.expect(payload)
    request.node._live_detail = f"{len(listed)} tools" + (f"; {evidence}" if evidence else "")
