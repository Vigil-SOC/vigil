"""One egress: every LLM call leaves through Bifrost, on one schema.

ADR 0011 decided this and nothing enforced it. `main` reached models four ways —
llm_gateway, llm_router, llm_format, llm_clients — plus provider-specific paths
inside claude_service and openai_agent_service. That count was not designed: each
was the reasonable local choice for one caller, and review did not stop it
reaching four.

A constraint that matters is expressed as something that fails.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("core", "services")

# Constructing a provider SDK client. Matched on the callee name, because the
# import spelling varies and the construction is what costs money.
CLIENTS = {"Anthropic", "AsyncAnthropic", "OpenAI", "AsyncOpenAI"}

# The sanctioned sites, and why each one is allowed to build a client.
ALLOWED_CONSTRUCTION = {
    # The single source of truth for Anthropic client construction.
    "core/llm/providers/clients.py",
    # The one OpenAI-shaped egress: every dispatch and stream goes out here.
    "core/llm/router/router.py",
}

# A provider host named in a base_url. Reaching one directly is the bypass this
# ratchet exists to catch, whatever client is used to do it.
PROVIDER_HOSTS = (
    "api.anthropic.com",
    "api.openai.com",
    "generativelanguage.googleapis.com",
)

# Where naming a provider host is the point rather than a bypass.
ALLOWED_HOSTS = {
    # Describes providers to the UI; dials none of them.
    "core/llm/providers/registry.py",
    "core/llm/providers/discovery.py",
    # The SSRF allow-list. It names these hosts to *refuse* everything else,
    # which is the opposite of a bypass.
    "core/platform/url_safety.py",
}


def python_files() -> list[Path]:
    files: list[Path] = []
    for package in PACKAGES:
        for path in (REPO_ROOT / package).rglob("*.py"):
            if any(
                part in ("__pycache__", "node_modules", "venv") for part in path.parts
            ):
                continue
            files.append(path)
    return sorted(files)


def _relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def constructions_in(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else getattr(node.func, "attr", "")
        )
        if name in CLIENTS:
            found.append((node.lineno, name))
    return found


def _docstrings(tree: ast.AST) -> set[int]:
    """Prose about an egress is not an egress."""
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            lines.add(first.value.lineno)
    return lines


def hosts_in(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return []

    prose = _docstrings(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if node.lineno in prose:
            continue
        for host in PROVIDER_HOSTS:
            if host in node.value:
                found.append((node.lineno, host))
    return found


@pytest.mark.unit
def test_no_provider_client_is_built_outside_the_sanctioned_sites():
    violations = [
        f"{_relative(path)}:{line}: builds {name} directly"
        for path in python_files()
        if _relative(path) not in ALLOWED_CONSTRUCTION
        for line, name in constructions_in(path)
    ]
    assert not violations, (
        "A provider client was constructed outside the one egress. Dispatch through "
        "core/llm/router/router.py, or in the harness through services/agent/core/wire.ts:\n  "
        + "\n  ".join(violations)
    )


@pytest.mark.unit
def test_no_provider_host_is_dialled_directly():
    violations = [
        f"{_relative(path)}:{line}: names {host}"
        for path in python_files()
        if _relative(path) not in ALLOWED_HOSTS
        for line, host in hosts_in(path)
    ]
    assert not violations, (
        "A provider host was named outside the key-validation exception. All traffic "
        "goes to BIFROST_URL; the gateway decides which provider it reaches:\n  "
        + "\n  ".join(violations)
    )


@pytest.mark.unit
def test_the_allow_lists_stay_honest():
    # An entry that stops constructing a client, or stops existing, must come off
    # the list -- otherwise the ratchet quietly widens as files are refactored.
    stale = [
        entry
        for entry in ALLOWED_CONSTRUCTION
        if not (REPO_ROOT / entry).exists() or not constructions_in(REPO_ROOT / entry)
    ]
    assert (
        not stale
    ), "ALLOWED_CONSTRUCTION entries no longer build a client:\n  " + "\n  ".join(stale)
