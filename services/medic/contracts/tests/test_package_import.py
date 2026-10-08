"""A3 F-16 / S0 review R15: the contracts import as a package once copied into Vigil."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
MODULES = [
    "decision_chain",
    "fingerprint_ref",
    "lane_ref",
    "pack_build",
    "pack_check",
    "rule_check",
    "trust_check",
    "vector_expand",
]


def test_every_module_imports_inside_a_package(tmp_path: Path) -> None:
    pkg = tmp_path / "medic" / "contracts"
    pkg.mkdir(parents=True)
    for parent in (tmp_path / "medic", pkg):
        (parent / "__init__.py").write_text("")
    for f in ROOT.iterdir():
        if f.suffix in (".py", ".json") or f.name.endswith(".md"):
            shutil.copy(f, pkg / f.name)
    code = "; ".join(f"import medic.contracts.{m}" for m in MODULES)
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(tmp_path)!r}); {code}",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert out.returncode == 0, out.stderr
