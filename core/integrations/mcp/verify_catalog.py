"""Offline packaged-catalog smoke check; never connects to vendor servers.

Run with ``python -m core.integrations.mcp.verify_catalog`` inside the backend image.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

# Entries whose launcher the image does not ship: Okta runs through docker.
# Joe Sandbox's uv is here, but its separate install is not, and this check
# only sees launchers.
NOT_SHIPPED = frozenset({"okta"})


def check_catalog(root: Path) -> dict:
    from core.integrations.mcp.service import MCPService

    entries = json.loads((root / "mcp-config.json").read_text())["mcpServers"]
    expected = {name for name, value in entries.items() if isinstance(value, dict)}
    if not expected:
        raise RuntimeError("Packaged connector catalog is empty")
    service = MCPService(
        project_root=root,
        integration_bridge=SimpleNamespace(derive_remote_mcp_env=lambda: {}),
        detection_rules=SimpleNamespace(get_mcp_env_vars=lambda: {}),
    )
    actual = set(service.list_servers())
    if actual != expected:
        raise RuntimeError(
            f"Catalog mismatch: missing={expected - actual}, extra={actual - expected}"
        )
    # MCPService swaps npx/uvx for the image's baked copy only when that copy
    # is the pinned version, so a command still npx/uvx is a pin the image lacks.
    unbaked = sorted(
        name for name, s in service.servers.items() if s.command in ("npx", "uvx")
    )
    if unbaked:
        raise RuntimeError(
            f"Catalog entries not installed in the image at their pinned version: {unbaked}"
        )
    missing = []
    for name, server in service.servers.items():
        command = server.command
        if not (Path(command).is_file() or shutil.which(command)):
            missing.append(name)
        elif any("git+" in arg for arg in server.args) and not shutil.which("git"):
            missing.append(name)
    unexpected = sorted(set(missing) - NOT_SHIPPED)
    if unexpected:
        raise RuntimeError(f"Catalog entries with no launcher: {unexpected}")
    return {
        "catalog_entries": len(actual),
        "missing_runtime_prerequisites": sorted(missing),
    }


if __name__ == "__main__":
    print(
        json.dumps(check_catalog(Path(__file__).resolve().parents[3]), sort_keys=True)
    )
