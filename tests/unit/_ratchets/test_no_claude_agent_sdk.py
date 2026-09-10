"""claude-agent-sdk is gone. Dependabot must not propose bumps against it.

#471 dropped the SDK and ran agents on the neutral loop; #638 landed the loop.
The PyPI pin and ``MCPRegistry.get_agent_sdk_configs`` were leftover. #691 is
the current Dependabot bump of a package nothing imports.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.integrations.mcp.registry import MCPRegistry

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("core", "services", "tools", "scripts")
_REQUIREMENT_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
# PEP 503: claude_agent_sdk and Claude.Agent.SDK are the same package.
_NORMALIZE = re.compile(r"[-_.]+")

pytestmark = pytest.mark.unit


def _canonical(name: str) -> str:
    return _NORMALIZE.sub("-", name).lower()


def _requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        match = _REQUIREMENT_NAME.match(line)
        if match:
            names.add(_canonical(match.group(1)))
    return names


def _python_files():
    for package in PACKAGES:
        root = REPO_ROOT / package
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(REPO_ROOT)


def _imports_claude_agent_sdk(rel_path: Path) -> list[int]:
    source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(rel_path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name.split(".")[0] == "claude_agent_sdk" for alias in node.names
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] == "claude_agent_sdk":
                lines.append(node.lineno)
    return lines


def test_requirements_do_not_declare_claude_agent_sdk():
    declared = _requirement_names(REPO_ROOT / "requirements.txt")
    locked = _requirement_names(REPO_ROOT / "requirements.lock")
    assert "claude-agent-sdk" not in declared
    assert "claude-agent-sdk" not in locked


def test_nothing_imports_claude_agent_sdk():
    hits = [
        f"{rel}:{lineno}"
        for rel in _python_files()
        for lineno in _imports_claude_agent_sdk(rel)
    ]
    assert not hits, "claude_agent_sdk import survived the drop:\n" + "\n".join(hits)


def test_mcp_registry_has_no_agent_sdk_config_shaper():
    assert not hasattr(MCPRegistry, "get_agent_sdk_configs")


def test_claude_harness_does_not_claim_agent_sdk_support():
    source = (REPO_ROOT / "core" / "llm" / "harness" / "claude.py").read_text()
    assert "Agent SDK" not in source
