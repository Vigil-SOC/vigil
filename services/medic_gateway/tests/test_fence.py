"""The gateway stays small, stdlib-only and fenced off from the rest of the repo."""

from __future__ import annotations

import ast
import configparser
import subprocess
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
REPO_ROOT = PKG.parents[1]
SOURCES = sorted(PKG.glob("*.py"))
CONTRACT = "importlinter:contract:medic-gateway"


MAX_CODE_LINES = 670  # a ratchet: lower it when you can, never raise it unasked
# The allow lists are data, pinned line by line by test_outbound and test_inbound.
TABLES = {"OUTBOUND", "INBOUND"}


def _code_lines(path: Path) -> int:
    """Lines of code as formatted (ruff/black, 88 columns): no blanks, comments,
    docstrings or allow-list tables."""
    src = path.read_text()
    docs: set[int] = set()
    for node in ast.walk(ast.parse(src)):
        body = getattr(node, "body", None)
        first = body[0] if isinstance(body, list) and body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            docs.update(range(first.lineno, first.end_lineno + 1))
        targets = getattr(node, "targets", [])
        if any(getattr(t, "id", "") in TABLES for t in targets):
            docs.update(range(node.lineno, node.end_lineno + 1))
    return sum(
        1
        for i, ln in enumerate(src.splitlines(), 1)
        if ln.strip() and not ln.strip().startswith("#") and i not in docs
    )


def test_gateway_code_stays_small():
    """A3-2 asked for '≤ ~500 lines' so a reviewer can hold all of it; S5's logic
    lands at about 660 once formatted and complete (S5-1, decided 2026-10-07)."""
    n = sum(_code_lines(f) for f in SOURCES)
    assert n <= MAX_CODE_LINES, n


def test_stdlib_only():
    """SP1 ⚑5: no third-party parser or client; nothing to audit but the stdlib."""
    for f in SOURCES:
        for node in ast.walk(ast.parse(f.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                top = name.split(".")[0]
                ok = top in sys.stdlib_module_names or top == "__future__"
                ok = ok or name.startswith("services.medic_gateway")
                assert ok, f"{f.name}: imports {name}"


def _cfg() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";",))
    cfg.read(REPO_ROOT / ".importlinter")
    return cfg


def test_every_other_python_service_is_forbidden():
    contract = _cfg()[CONTRACT]
    assert contract["source_modules"].split() == ["services.medic_gateway"]
    forbidden = set(contract["forbidden_modules"].split())
    others = {
        f"services.{d.name}"
        for d in (REPO_ROOT / "services").iterdir()
        if (d / "__init__.py").exists() and d.name != "medic_gateway"
    }
    assert {"services.medic", "core", "tools"} <= forbidden
    assert others <= forbidden, f"add to .importlinter: {others - forbidden}"


def _lint(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    real = _cfg()
    cfg = configparser.ConfigParser()
    cfg["importlinter"] = real["importlinter"]
    cfg[CONTRACT] = real[CONTRACT]
    with open(tmp_path / ".importlinter", "w") as f:
        cfg.write(f)
    for module in real[CONTRACT]["forbidden_modules"].split() + [
        "services.medic_gateway"
    ]:
        pkg = tmp_path.joinpath(*module.split("."))
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
    (tmp_path / "services/medic_gateway/mod.py").write_text(source)
    return subprocess.run(
        [str(Path(sys.executable).parent / "lint-imports"), "--no-cache"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    "planted", ["import core", "import services.medic", "from tools import x"]
)
def test_fence_breaks_on_a_planted_import(tmp_path, planted):
    result = _lint(tmp_path, planted + "\n")
    assert result.returncode != 0 and "BROKEN" in result.stdout, result.stdout


def test_fence_keeps_a_clean_gateway(tmp_path):
    result = _lint(tmp_path, "import json\n")
    assert result.returncode == 0, result.stdout + result.stderr
