"""Config writes name a real actor.

HTTP handlers stamp the signed-in user. ``web_ui`` and ``api`` are placeholders,
and a write that calls ``get_config_service()`` with no actor is recorded as
``system``. The check looks at the functions that perform the write, not only
route handlers: the placeholders used to live in ``budget.py`` and
``federation/store.py``.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("services", "core")

PLACEHOLDER_ACTORS = {"web_ui", "api"}
WRITE_METHODS = {
    "set_system_config",
    "set_integration_config",
    "record_audit",
    "record_integration_test",
}
# These take the actor from the route and pass it into get_config_service.
ACTOR_FUNCS = {
    "get_config_service",
    "set_global_settings",
    "set_settings",
    "set_enabled",
}
ACTOR_PARAMS = {"user_id", "updated_by"}


def _python_files():
    for package in PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(REPO_ROOT)


def _callee_name(node: ast.AST):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _placeholder(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in PLACEHOLDER_ACTORS


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _local_nodes(func: ast.AST):
    """Body of ``func`` without nested function bodies."""
    nested: set[int] = set()
    for node in ast.walk(func):
        if node is func or not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        for child in ast.walk(node):
            nested.add(id(child))
    for node in ast.walk(func):
        if id(node) not in nested:
            yield node


def _placeholder_names(func: ast.AST) -> set[str]:
    """Parameters whose default is a placeholder actor."""
    names: set[str] = set()
    args = func.args
    positional = [arg.arg for arg in args.args]
    if args.defaults:
        start = len(positional) - len(args.defaults)
        for name, default in zip(positional[start:], args.defaults):
            if _placeholder(default):
                names.add(name)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None and _placeholder(default):
            names.add(arg.arg)
    return names


def _actor_omitted(call: ast.Call) -> bool:
    if _callee_name(call.func) != "get_config_service":
        return False
    if any(kw.arg == "user_id" for kw in call.keywords):
        return False
    return not call.args


def placeholder_actor_lines(tree: ast.AST) -> list[int]:
    """Lines that pass ``web_ui`` or ``api`` as the config actor."""
    found: list[int] = []
    for func in _functions(tree):
        bad_names = _placeholder_names(func)
        for node in _local_nodes(func):
            if not isinstance(node, ast.Call):
                continue
            name = _callee_name(node.func)
            if name not in ACTOR_FUNCS:
                continue
            for kw in node.keywords:
                if kw.arg not in ACTOR_PARAMS:
                    continue
                if _placeholder(kw.value):
                    found.append(node.lineno)
                elif isinstance(kw.value, ast.Name) and kw.value.id in bad_names:
                    found.append(node.lineno)
            if name != "set_settings":
                for arg in node.args:
                    if _placeholder(arg):
                        found.append(node.lineno)
    return found


def bare_write_lines(tree: ast.AST) -> list[int]:
    """Lines that write config through a ``get_config_service()`` with no actor."""
    found: list[int] = []
    for func in _functions(tree):
        bare: dict[str, bool] = {}
        for node in _local_nodes(func):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if _callee_name(node.value.func) == "get_config_service":
                    missing = _actor_omitted(node.value)
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            bare[target.id] = missing
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.value, ast.Call)
                and _callee_name(node.value.func) == "get_config_service"
                and isinstance(node.target, ast.Name)
            ):
                bare[node.target.id] = _actor_omitted(node.value)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in WRITE_METHODS
            ):
                value = node.func.value
                if isinstance(value, ast.Call) and _actor_omitted(value):
                    found.append(node.lineno)
                elif isinstance(value, ast.Name) and bare.get(value.id):
                    found.append(node.lineno)
    return found


def _violations(source: str):
    tree = ast.parse(source)
    return placeholder_actor_lines(tree), bare_write_lines(tree)


@pytest.mark.unit
def test_no_placeholder_or_anonymous_config_actor():
    violations = []
    for rel_path in _python_files():
        source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for lineno in placeholder_actor_lines(tree):
            violations.append(f"{rel_path}:{lineno}: placeholder actor web_ui/api")
        for lineno in bare_write_lines(tree):
            violations.append(
                f"{rel_path}:{lineno}: config write with no actor "
                "(falls through to system)"
            )
    assert not violations, (
        "A config write must name its actor. HTTP handlers pass "
        "str(current_user.user_id). Service writers pass their own name "
        "(system, approval_service), not web_ui or api.\n" + "\n".join(violations)
    )


@pytest.mark.unit
def test_ratchet_sees_placeholders_outside_a_handler():
    """budget.py and federation/store.py are not route handlers."""
    placeholders, _bare = _violations(
        "def set_settings():\n"
        "    get_config_service(user_id='api').set_system_config()\n"
        "def set_global_settings(value, updated_by='api'):\n"
        "    get_config_service(user_id=updated_by).set_system_config()\n"
    )
    assert placeholders == [2, 4]


@pytest.mark.unit
def test_ratchet_sees_a_handler_write_with_no_actor():
    _placeholders, bare = _violations(
        "async def put_kafka_config():\n"
        "    get_config_service().set_system_config()\n"
        "def set_enabled(enabled):\n"
        "    svc = get_config_service()\n"
        "    svc.set_system_config()\n"
    )
    assert bare == [2, 5]


@pytest.mark.unit
def test_ratchet_allows_a_named_service_actor_and_a_read():
    placeholders, bare = _violations(
        "def save():\n"
        "    get_config_service(user_id='approval_service').set_system_config()\n"
        "def load():\n"
        "    return get_config_service().get_system_config('approval.force_manual_approval')\n"
    )
    assert placeholders == []
    assert bare == []
