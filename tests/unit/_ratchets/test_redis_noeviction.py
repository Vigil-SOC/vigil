"""Redis holds queues and dedup state, so it must reject writes when full.

Issue #1693: ``allkeys-lru`` silently evicts ARQ jobs, dedup sets and session
keys under memory pressure. Every shipped Redis must run ``noeviction``.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import List

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_FILES = (
    REPO / "infra" / "docker" / "docker-compose.yml",
    REPO / "clients" / "desktop" / "standalone" / "docker-compose.yml",
)
HELM_VALUES = REPO / "infra" / "helm" / "vigil" / "values.yaml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _policy(argv: List[str]) -> str | None:
    if "--maxmemory-policy" not in argv:
        return None
    return argv[argv.index("--maxmemory-policy") + 1]


def _compose_redis_argv(path: Path) -> List[str]:
    command = _load(path)["services"]["redis"]["command"]
    return shlex.split(command) if isinstance(command, str) else list(command)


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.parent.name)
def test_compose_redis_uses_noeviction(path: Path) -> None:
    assert _policy(_compose_redis_argv(path)) == "noeviction"


def test_helm_redis_uses_noeviction() -> None:
    args = [str(a) for a in _load(HELM_VALUES)["redis"]["args"]]
    assert _policy(args) == "noeviction"
