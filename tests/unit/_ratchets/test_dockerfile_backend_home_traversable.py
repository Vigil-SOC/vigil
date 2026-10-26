"""The backend image's /home/vigil must be traversable by any host uid.

Debian 13's `useradd -m` makes the home 0700 (HOME_MODE). `start.sh`'s backup
one-shots run as the host uid with the State Directory mounted under
/home/vigil/.vigil, so a 0700 parent makes them fail with PermissionError.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

DOCKERFILE = Path(__file__).resolve().parents[3] / "infra" / "docker" / "Dockerfile.backend"
# 0755 / 755 / a+x / o+x on /home/vigil
CHMOD = re.compile(r"chmod\s+(?:0?755|a\+rx|a\+x|o\+rx|o\+x)\s+/home/vigil(?:\s|$|&|;)")


def test_home_made_traversable_after_useradd() -> None:
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if "useradd" in ln and "vigil" in ln)
    after = "\n".join(lines[start + 1 :]).replace("\\\n", " ")
    assert CHMOD.search(after), "chmod 0755 /home/vigil must follow the useradd -m of vigil"
