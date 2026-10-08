"""The .importlinter fence names every other Python service (A3-1).

A forbidden contract can't say "services.* except Medic", so it lists them. This
fails when a new Python service lands without being added to that list.
"""

from __future__ import annotations

import configparser
from pathlib import Path

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
