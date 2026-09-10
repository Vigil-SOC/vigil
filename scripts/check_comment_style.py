#!/usr/bin/env python3
# Gates the agent layer's comment style: no comment block over two lines, and no
# docstrings. Prose that needs more room does not belong in the code at all.

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

MAX_LINES = 2
ROOT = Path(__file__).resolve().parents[1]

# Only what the agent-loop work owns. Widen deliberately, never by accident:
# core/agents and core/llm predate this and carry docstrings throughout.
SCOPED = (
    "services/agent",
    "core/agents/queue.py",
    "core/agents/agent_runs_router.py",
    "core/agents/tool_registry.py",
    "core/agents/tools_router.py",
    "core/llm/cost/rates.py",
    "core/agents/directives.py",
    "core/agents/internal_auth.py",
    "core/response/checkpoints.py",
    "core/workflows/run_bridge_router.py",
    "core/workflows/run_resume.py",
    "infra/database/init/19_agent_ledger.sql",
    "infra/database/init/20_agent_directives.sql",
    "infra/database/init/21_agent_run_leases.sql",
    "infra/database/init/31_agent_ledger_hash_chain.sql",
)
SKIP = ("node_modules", "package-lock.json", "dist", "__pycache__")

LINE_COMMENT = re.compile(r"^\s*(//|#|--)")
SHEBANG = re.compile(r"^#!")


def scoped_files() -> list[Path]:
    found: list[Path] = []
    for entry in SCOPED:
        target = ROOT / entry
        if target.is_file():
            found.append(target)
        elif target.is_dir():
            found.extend(p for p in target.rglob("*") if p.is_file())
    return [p for p in found if p.suffix in {".ts", ".py", ".sql"} and not any(s in p.parts or s == p.name for s in SKIP)]


def long_blocks(path: Path) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = count = 0
    for number, line in enumerate(path.read_text(encoding="utf8").splitlines(), 1):
        if LINE_COMMENT.match(line) and not SHEBANG.match(line):
            if not count:
                start = number
            count += 1
            continue
        if count > MAX_LINES:
            runs.append((start, count))
        count = 0
    if count > MAX_LINES:
        runs.append((start, count))
    return runs


def docstrings(path: Path) -> list[int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf8"))
    except SyntaxError:
        return []
    nodes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return [node.body[0].lineno for node in ast.walk(tree) if isinstance(node, nodes) and ast.get_docstring(node)]


def main() -> int:
    failures: list[str] = []
    for path in sorted(scoped_files()):
        rel = path.relative_to(ROOT)
        for start, count in long_blocks(path):
            failures.append(f"{rel}:{start}: comment block of {count} lines (max {MAX_LINES})")
        if path.suffix == ".py":
            for line in docstrings(path):
                failures.append(f"{rel}:{line}: docstring (use a comment of {MAX_LINES} lines or fewer)")

    for failure in failures:
        print(failure, file=sys.stderr)
    print(f"comment style: {len(failures)} violation(s)", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
