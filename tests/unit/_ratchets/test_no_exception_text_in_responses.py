"""A catch-all handler must not put the exception's text in the response.

``str(e)`` of a database or upstream failure carries hosts, users, SQL and
provider response bodies. Let unexpected errors reach the global handler
(``services/api/errors.py``), which logs them and returns a trace id, or answer
with a fixed message. Typed domain errors (``except ValueError``) are written
for the client and are not covered.
"""

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Operator-facing diagnostics that deliberately return their reason.
ALLOWED = {
    ("core/workflows/workflows_router.py", "narrate_workflow_run"),
    ("core/workflows/workflows_router.py", "replay_workflow_run"),
    ("core/workflows/workflows_router.py", "verify_workflow_run"),
    ("services/api/routers/cases.py", "get_case_record"),
    ("services/api/routers/storage_status.py", "reconnect_database"),  # admin only
}
_RESPONSE_KEYS = {"error", "message", "detail"}
_CATCH_ALL = {"Exception", "BaseException"}


def _scanned():
    yield from (_REPO_ROOT / "services" / "api").rglob("*.py")
    yield from (_REPO_ROOT / "core").rglob("*router*.py")


def _uses(node: ast.AST, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))


def _embeds_exception(expr: ast.AST, name: str) -> bool:
    for n in ast.walk(expr):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in {"str", "repr"}
            and n.args
            and _uses(n.args[0], name)
        ):
            return True
        if isinstance(n, ast.FormattedValue) and _uses(n.value, name):
            return True
    return False


def _response_values(handler: ast.ExceptHandler):
    for n in ast.walk(handler):
        if isinstance(n, ast.Call):
            callee = getattr(n.func, "id", getattr(n.func, "attr", None))
            if callee == "HTTPException":
                yield from (k.value for k in n.keywords if k.arg == "detail")
        elif isinstance(n, ast.Dict):
            for key, value in zip(n.keys, n.values):
                if isinstance(key, ast.Constant) and key.value in _RESPONSE_KEYS:
                    yield value


def _violations(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(_REPO_ROOT).as_posix()

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, child.name)
                continue
            if isinstance(child, ast.ExceptHandler) and child.name:
                catch_all = child.type is None or (
                    isinstance(child.type, ast.Name) and child.type.id in _CATCH_ALL
                )
                if (
                    catch_all
                    and (rel, func) not in ALLOWED
                    and any(
                        _embeds_exception(v, child.name)
                        for v in _response_values(child)
                    )
                ):
                    yield f"{rel}:{child.lineno}"
            yield from walk(child, func)

    yield from walk(tree, None)


@pytest.mark.unit
def test_catch_all_handlers_do_not_return_exception_text():
    violations = [v for path in _scanned() for v in _violations(path)]
    assert not violations, (
        "A catch-all handler returns str(e) / {e} to the client. Delete the "
        "handler so the global one answers, or return a fixed message:\n"
        + "\n".join(violations)
    )
