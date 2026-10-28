"""report_outage / report_recovered take a string-literal message.

Reminder and recovery are that literal plus a fixed suffix, so a pipeline can
match one outage by template. A variable or an f-string would put the outage's
identity back in an argument. Adjacent string literals count: the parser folds
them into one Constant.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("core", "services", "tools", "scripts")
_FUNCS = {"report_outage", "report_recovered"}


def _calls():
    for package in PACKAGES:
        root = REPO_ROOT / package
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _func_name(node.func) in _FUNCS:
                    yield rel, node


def _func_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _message_arg(call: ast.Call) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == "message":
            return keyword.value
    if len(call.args) >= 3:
        return call.args[2]
    return None


def _is_string_constant(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _violations() -> list[str]:
    hits = []
    for rel, call in _calls():
        if not _is_string_constant(_message_arg(call)):
            name = _func_name(call.func)
            hits.append(f"{rel}:{call.lineno} {name} message is not a string literal")
    return hits


@pytest.mark.unit
def test_outage_messages_are_string_literals():
    hits = _violations()
    assert hits == [], (
        "report_outage/report_recovered message must be a string literal "
        "(adjacent literals count):\n" + "\n".join(hits)
    )
