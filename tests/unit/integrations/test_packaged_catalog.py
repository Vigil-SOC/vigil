"""A runtime layout without a repository venv must still load usable commands."""

import json
import shutil
import sys
from types import SimpleNamespace

import pytest

from core.integrations.mcp import packaged
from core.integrations.mcp.service import MCPService
from core.integrations.mcp.verify_catalog import check_catalog


def test_catalog_loads_without_a_project_virtualenv(tmp_path):
    (tmp_path / "mcp-config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fixture": {"command": "python3", "args": ["-m", "fixture"]}
                }
            }
        )
    )
    assert check_catalog(tmp_path) == {
        "catalog_entries": 1,
        "missing_runtime_prerequisites": [],
    }
    service = MCPService(
        project_root=tmp_path,
        integration_bridge=SimpleNamespace(derive_remote_mcp_env=lambda: {}),
        detection_rules=SimpleNamespace(get_mcp_env_vars=lambda: {}),
    )
    assert service.servers["fixture"].command == sys.executable


def test_existing_project_virtualenv_remains_selected(tmp_path):
    venv_python = (
        tmp_path
        / "venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    (tmp_path / "mcp-config.json").write_text('{"mcpServers": {}}')
    service = MCPService(
        project_root=tmp_path,
        integration_bridge=SimpleNamespace(derive_remote_mcp_env=lambda: {}),
        detection_rules=SimpleNamespace(get_mcp_env_vars=lambda: {}),
    )
    assert service.python_exe == venv_python


def _baked(tmp_path, monkeypatch):
    """Fake image layout: one npm package and one uv tool venv at pinned versions."""
    npm = tmp_path / "npm" / "node_modules" / "@scope" / "srv"
    npm.mkdir(parents=True)
    (npm / "package.json").write_text(
        json.dumps({"version": "1.2.3", "bin": {"srv": "dist/index.js"}})
    )
    venv = tmp_path / "tools" / "falcon-mcp"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "falcon-mcp").touch()
    site = venv / "lib" / "python3.12" / "site-packages"
    for name, version in [("falcon_mcp", "0.19.0"), ("mcp", "1.29.1")]:
        info = site / f"{name}-{version}.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n")
    monkeypatch.setattr(packaged, "NPM_PREFIX", tmp_path / "npm")
    monkeypatch.setattr(packaged, "UV_TOOL_DIR", tmp_path / "tools")
    return npm, venv


def test_baked_pins_launch_the_installed_copy(tmp_path, monkeypatch):
    npm, venv = _baked(tmp_path, monkeypatch)
    launch = packaged.installed_launch
    assert launch("npx", ["-y", "@scope/srv@1.2.3", "--flag"]) == (
        "node",
        [str(npm / "dist" / "index.js"), "--flag"],
    )
    assert launch("uvx", ["--with", "mcp==1.29.1", "falcon-mcp==0.19.0", "-v"]) == (
        str(venv / "bin" / "falcon-mcp"),
        ["-v"],
    )
    # Any other version, or a spec not baked, keeps the declared launch.
    for command, args in [
        ("npx", ["-y", "@scope/srv@1.2.4"]),
        ("uvx", ["falcon-mcp==0.20.0"]),
        ("uvx", ["--with", "mcp==2.0.0", "falcon-mcp==0.19.0"]),
        ("uvx", ["--from", "other==1.0", "falcon-mcp"]),
    ]:
        assert launch(command, args) == (command, args)


def test_bump_without_image_list_fails_verify_catalog(tmp_path, monkeypatch):
    _baked(tmp_path, monkeypatch)
    entries = {
        "ok": {"command": "npx", "args": ["-y", "@scope/srv@1.2.3"]},
        "bumped": {"command": "uvx", "args": ["falcon-mcp==0.20.0"]},
    }
    (tmp_path / "mcp-config.json").write_text(json.dumps({"mcpServers": entries}))
    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    with pytest.raises(RuntimeError, match=r"pinned version: \['bumped'\]"):
        check_catalog(tmp_path)
