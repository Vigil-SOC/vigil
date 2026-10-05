"""Every redirect that writes a logs/*.log file goes through ``rotate_log``.

A bare ``> logs/x.log`` destroys the previous run's log, which a support
bundle can only collect if it survives. The redirect must be preceded, in the
same file and within a few lines, by ``rotate_log`` on the same path. Appends
(``>>``) never destroy anything, so they need rotating only where listed below.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = [
    REPO / "start.sh",
    REPO / "shutdown_all.sh",
    *sorted((REPO / "scripts").glob("*.sh")),
]

# `>` or `>>` to a .log path; `2>&1`, `>&2` and `&>` do not match.
REDIRECT = re.compile(
    r'(?<![\d&<>])(>>?)\s*"?([^\s">&]*(?:logs|\$log\b)[^\s">]*?(?:\.log)?)"?(?=\s|$)'
)
WINDOW = 8

# Appends that deliberately keep one growing file: install_dev_deps adds to the
# pip log install_python_deps already rotated.
APPEND_ALLOWLIST = {("scripts/lib.sh", "$log")}


def _sites():
    for path in SCRIPTS:
        rel = path.relative_to(REPO).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                continue
            for op, target in REDIRECT.findall(line):
                if not target.endswith(".log") and target != "$log":
                    continue
                yield rel, number, op, target, lines


def test_every_log_redirect_rotates_first() -> None:
    sites = list(_sites())
    assert sites, "ratchet matched nothing; the redirect pattern is stale"
    missing = []
    for rel, number, op, target, lines in sites:
        if op == ">>" and (rel, target) in APPEND_ALLOWLIST:
            continue
        before = lines[max(0, number - WINDOW) : number + 1]
        if not any("rotate_log" in line and target in line for line in before):
            missing.append(f"{rel}:{number + 1}: {op} {target}")
    assert not missing, "log redirect without rotate_log:\n" + "\n".join(missing)
