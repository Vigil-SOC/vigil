"""The .importlinter fence names every other Python service (A3-1).

A forbidden contract can't say "services.* except Medic", so it lists them. This
fails when a new Python service lands without being added to that list.
"""

from __future__ import annotations

import configparser
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_every_other_python_service_is_forbidden() -> None:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";",))
    cfg.read(REPO_ROOT / ".importlinter")
    contract = cfg["importlinter:contract:medic"]
    assert contract["source_modules"].split() == ["services.medic"]
    forbidden = set(contract["forbidden_modules"].split())

    services = {
        f"services.{d.name}"
        for d in (REPO_ROOT / "services").iterdir()
        if (d / "__init__.py").exists() and d.name != "medic"
    }
    assert services, "found no Python services; is REPO_ROOT right?"
    assert services <= forbidden, f"add to .importlinter: {services - forbidden}"
    assert {"core", "tools"} <= forbidden


def _lint_imports(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(Path(sys.executable).parent / "lint-imports"), "--no-cache"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _tree(tmp_path: Path, medic_source: str) -> Path:
    """A minimal repo: the real Medic contract, stub packages, one Medic module."""
    real = configparser.ConfigParser(inline_comment_prefixes=(";",))
    real.read(REPO_ROOT / ".importlinter")
    cfg = configparser.ConfigParser()
    cfg["importlinter"] = real["importlinter"]
    cfg["importlinter:contract:medic"] = real["importlinter:contract:medic"]
    with open(tmp_path / ".importlinter", "w") as f:
        cfg.write(f)
    forbidden = real["importlinter:contract:medic"]["forbidden_modules"].split()
    for module in forbidden + ["services.medic"]:
        pkg = tmp_path.joinpath(*module.split("."))
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
    (tmp_path / "services/medic/mod.py").write_text(medic_source)
    return tmp_path


@pytest.mark.parametrize(
    "planted", ["import core", "from tools import x", "import services.daemon"]
)
def test_fence_breaks_on_a_planted_import(tmp_path: Path, planted: str) -> None:
    result = _lint_imports(_tree(tmp_path, planted + "\n"))
    assert result.returncode != 0, result.stdout
    assert "Medic imports nothing" in result.stdout and "BROKEN" in result.stdout


def test_fence_keeps_a_clean_medic(tmp_path: Path) -> None:
    result = _lint_imports(_tree(tmp_path, "import json\n"))
    assert result.returncode == 0, result.stdout + result.stderr
