"""Redis clients and Redis URLs are built only in ``core/redis_client.py``.

Call sites may hold a client the helper returned. They may not read
``redis_url``, call ``from_url`` / ``redis.Redis`` / ``from_dsn``, or pass
``storage_uri`` themselves. ``gateway.py`` may call ``create_pool(settings)``
with the settings object the helper already built.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("core", "services")
HELPER = "core/redis_client.py"
GATEWAY = "core/llm/gateway/gateway.py"
CALLS = {"from_url", "from_dsn", "create_pool", "Redis"}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _allowed_call(rel: str, node: ast.Call, name: str) -> bool:
    if rel == HELPER:
        return True
    if rel != GATEWAY or name != "create_pool":
        return False
    if node.keywords or len(node.args) != 1:
        return False
    arg = node.args[0]
    return isinstance(arg, ast.Name) and arg.id == "settings"


def violations() -> list[str]:
    found: list[str] = []
    for package in PACKAGES:
        root = REPO_ROOT / package
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(REPO_ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = _call_name(node)
                    if name in CALLS and not _allowed_call(rel, node, name):
                        found.append(f"{rel}:{node.lineno} {name}()")
                    for keyword in node.keywords:
                        if keyword.arg == "storage_uri" and rel != HELPER:
                            found.append(f"{rel}:{node.lineno} storage_uri=")
                elif (
                    isinstance(node, ast.Attribute)
                    and node.attr == "redis_url"
                    and rel != HELPER
                ):
                    found.append(f"{rel}:{node.lineno} .redis_url")
    return found


@pytest.mark.unit
def test_redis_clients_come_from_the_helper():
    found = violations()
    assert (
        not found
    ), "Build Redis clients and URLs in core/redis_client.py:\n  " + "\n  ".join(found)
