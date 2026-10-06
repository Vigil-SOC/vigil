"""The count of f-string log calls at warning or above may only go down.

``record.msg`` is the grouping key for a log line (see ``core.telemetry``);
an f-string bakes the variable data into it, so every value is a new "kind".
Use ``logger.error("... %s", value)`` instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("core", "services", "tools", "scripts")
LEVELS = {"warning", "error", "exception", "critical"}

# Lower this when converting calls; the test fails if the count differs.
BASELINE = 360


def _fstring_log_calls() -> list[str]:
    hits = []
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
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in LEVELS
                    and node.args
                    and isinstance(node.args[0], ast.JoinedStr)
                ):
                    hits.append(f"{rel}:{node.lineno}")
    return hits


@pytest.mark.unit
def test_fstring_log_calls_do_not_grow():
    count = len(_fstring_log_calls())
    assert count <= BASELINE, (
        f"{count} f-string logger.warning/error/exception/critical calls, "
        f"baseline is {BASELINE}. Use %-style arguments "
        "(logger.error('failed for %s', x)) so the message template stays "
        "constant."
    )
    assert count == BASELINE, (
        f"Only {count} f-string log calls remain; lower BASELINE in this file "
        f"from {BASELINE} to {count} so the gain is locked in."
    )
