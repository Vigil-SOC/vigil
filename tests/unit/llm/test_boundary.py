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
UPWARD_PACKAGES = ("services",)

# Was grandfathered for backend/schemas/tool_schemas.py; r9a moved that file to
# core/llm/tool_schemas.py, so the import is no longer upward and the
# exemption is obsolete.
UPWARD_ALLOWED: set[tuple[str, str]] = set()

SOURCES = sorted(LLM_ROOT.rglob("*.py"))
ROUTER_SOURCES = [
    p for p in SOURCES if p.relative_to(LLM_ROOT).parts[0] in FORBIDDEN_EDGES
]


def _package(source):
    """Dotted package a module's relative imports resolve against."""
    return ".".join(source.parent.relative_to(REPO_ROOT).parts)


def _imports(node, package):
    """(lineno, absolute dotted name) for each module an import node pulls in."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield node.lineno, alias.name
        return
    if not isinstance(node, ast.ImportFrom):
        return
    if not node.level:
        yield node.lineno, node.module
        return
    base = package.rsplit(".", node.level - 1)[0]  # level 1 is the package itself
    if node.module:
        yield node.lineno, f"{base}.{node.module}"
    else:
        for alias in node.names:  # `from .. import harness` — the name is the module
            yield node.lineno, f"{base}.{alias.name}"


def _module_scope_imports(tree, package):
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield from _imports(node, package)
        stack.extend(ast.iter_child_nodes(node))


def _all_imports(tree, package):
    for node in ast.walk(tree):
        yield from _imports(node, package)


@pytest.mark.parametrize("source", SOURCES, ids=lambda p: p.name)
def test_no_module_scope_import_of_deployables(source):
    rel = source.relative_to(LLM_ROOT).as_posix()
    offenders = [
        (lineno, module)
        for lineno, module in _module_scope_imports(
            ast.parse(source.read_text()), _package(source)
        )
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
        for lineno, module in _all_imports(
            ast.parse(source.read_text()), _package(source)
        )
        if module.startswith(forbidden)
    ]
    assert not offenders, f"{rel} must not import {forbidden}: {offenders}"


# Both ratchets match on absolute dotted names, so a relative import only counts
# if it resolves first. Without this the router could reach the harness unseen.
@pytest.mark.parametrize(
    "source,expected",
    [
        ("from ..harness import ClaudeService", "core.llm.harness"),
        ("from ..harness.claude import ClaudeService", "core.llm.harness.claude"),
        ("from .. import harness", "core.llm.harness"),
        ("from .format import to_openai", "core.llm.router.format"),
        ("from ..security import sanitize", "core.llm.security"),
    ],
)
def test_relative_imports_resolve_to_absolute(source, expected):
    resolved = [m for _, m in _all_imports(ast.parse(source), "core.llm.router")]
    assert resolved == [expected]
