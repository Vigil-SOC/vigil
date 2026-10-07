"""Forbid blocking calls written directly in an ``async def`` route handler.

#461 settled the execution model: plain ``def`` is the default (FastAPI runs it
in the threadpool), and an ``async def`` route exists only because it must
``await``. A synchronous DB, file, subprocess, sleep or bcrypt call in its body
then runs on the event loop and stalls every other request on the worker
(#1411). Push that section through ``await asyncio.to_thread(fn, ...)``, or
make the route ``def`` if it awaits nothing.

Only the handler's own body is checked: calls into services are not followed,
and a nested ``def``/``lambda`` is not a direct call (it is how a sync section
is handed to ``asyncio.to_thread``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("core", "services")

ROUTE_DECORATORS = {"get", "post", "put", "patch", "delete", "api_route", "websocket"}
SESSION_METHODS = {"query", "execute", "add", "delete", "flush"}
BLOCKING_MODULES = {"subprocess", "bcrypt"}


def _python_files():
    for package in PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(REPO_ROOT)


def _is_route(fn: ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and d.func.attr in ROUTE_DECORATORS
        for d in fn.decorator_list
    )


def _last_name(node: ast.AST):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _blocking_kind(call: ast.Call):
    func = call.func
    if isinstance(func, ast.Name):
        return "open()" if func.id == "open" else None
    if not isinstance(func, ast.Attribute):
        return None
    owner = _last_name(func.value)
    if owner is None:
        return None
    if owner in BLOCKING_MODULES:
        return f"{owner}.{func.attr}"
    if owner == "time" and func.attr == "sleep":
        return "time.sleep"
    if func.attr in SESSION_METHODS and "session" in owner.lower():
        return f"{owner}.{func.attr}"
    return None


def _direct_calls(fn: ast.AsyncFunctionDef):
    """Calls in ``fn``'s own body, not inside nested functions or lambdas."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _violations(source: str):
    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef) or not _is_route(fn):
            continue
        for call in _direct_calls(fn):
            kind = _blocking_kind(call)
            if kind:
                yield call.lineno, fn.name, kind


@pytest.mark.unit
def test_no_blocking_calls_in_async_routes():
    violations = [
        f"{rel}:{lineno}: {func}() calls {kind}"
        for rel in _python_files()
        for lineno, func, kind in _violations(
            (REPO_ROOT / rel).read_text(encoding="utf-8")
        )
    ]
    assert not violations, (
        "Blocking call on the event loop in an async route. Make the route a "
        "plain `def` if it awaits nothing, or wrap the sync section in "
        "`await asyncio.to_thread(...)` (#461, #1411).\n"
        + "\n".join(sorted(violations))
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "body, kind",
    [
        ("session.query(User).first()", "session.query"),
        ("db_session.flush()", "db_session.flush"),
        ("open('/etc/hosts').read()", "open()"),
        ("subprocess.run(['git', 'clone', url])", "subprocess.run"),
        ("time.sleep(1)", "time.sleep"),
        ("bcrypt.hashpw(pw, salt)", "bcrypt.hashpw"),
    ],
)
def test_fails_on_a_planted_blocking_call(body, kind):
    source = f"""
@router.post("/x")
async def handler(session):
    await revoke()
    {body}
"""
    assert [k for _, _, k in _violations(source)] == [kind]


@pytest.mark.unit
def test_leaves_offloaded_and_sync_routes_alone():
    source = """
@router.post("/x")
async def offloaded(session):
    user = await asyncio.to_thread(_get_user, session, "u1")
    await asyncio.to_thread(lambda: session.query(User).all())

@router.get("/y")
def sync_route(session):
    return session.query(User).all()

async def not_a_route(session):
    session.query(User).all()
"""
    assert list(_violations(source)) == []
