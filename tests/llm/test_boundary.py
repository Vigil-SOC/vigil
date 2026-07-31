import ast

import pytest

from core.config import REPO_ROOT

pytestmark = pytest.mark.unit

LLM_ROOT = REPO_ROOT / "core" / "llm"

# Sub-package -> module prefixes it must never import, at any scope. The router
# runs one stateless completion; the harness loops over it. Dependency is one-way.
FORBIDDEN_EDGES = {"router": ("core.llm.harness",)}

# core/llm sits below the deployables, so importing them at module scope inverts
# the layering. Lazy in-function imports are the sanctioned escape hatch.
UPWARD_PACKAGES = ("backend", "daemon")

# Grandfathered: ClaudeService reads the backend tool schemas at import time
# behind a try/ImportError. Removing it is #414's job, not the relocation's.
UPWARD_ALLOWED = {("harness/claude.py", "backend.schemas.tool_schemas")}

SOURCES = sorted(LLM_ROOT.rglob("*.py"))
ROUTER_SOURCES = [
    p for p in SOURCES if p.relative_to(LLM_ROOT).parts[0] in FORBIDDEN_EDGES
]


def _imports(node):
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        yield node.lineno, node.module
    elif isinstance(node, ast.Import):
        for alias in node.names:
            yield node.lineno, alias.name


def _module_scope_imports(tree):
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield from _imports(node)
        stack.extend(ast.iter_child_nodes(node))


def _all_imports(tree):
    for node in ast.walk(tree):
        yield from _imports(node)


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_module_scope_import_of_deployables(source):
    rel = source.relative_to(LLM_ROOT).as_posix()
    offenders = [
        (lineno, module)
        for lineno, module in _module_scope_imports(ast.parse(source.read_text()))
        if module.split(".")[0] in UPWARD_PACKAGES
        and (rel, module) not in UPWARD_ALLOWED
    ]
    assert not offenders, (
        f"{rel} imports a deployable at module scope: {offenders}. "
        "Defer it into the function that needs it."
    )


@pytest.mark.parametrize("source", ROUTER_SOURCES, ids=lambda p: p.name)
def test_subpackage_dependency_direction(source):
    rel = source.relative_to(LLM_ROOT).as_posix()
    forbidden = FORBIDDEN_EDGES[rel.split("/")[0]]
    offenders = [
        (lineno, module)
        for lineno, module in _all_imports(ast.parse(source.read_text()))
        if module.startswith(forbidden)
    ]
    assert not offenders, f"{rel} must not import {forbidden}: {offenders}"
