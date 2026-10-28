"""Compose agent-worker and agent-serve healthchecks must probe /readyz.

Issue #2100: `/healthz` always answers ok, so a worker that lost Redis or a
serve that lost Postgres stayed `healthy` in `docker ps`. `/readyz` returns 503
when that dependency is down. Helm already uses /readyz for readiness.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_FILES = [
    REPO / "infra" / "docker" / "docker-compose.yml",
    REPO / "clients" / "desktop" / "standalone" / "docker-compose.yml",
]
SERVICES = {"agent-worker": 6990, "agent-serve": 6989}
MIN_START_PERIOD_S = 30

CASES = [
    pytest.param(path, svc, port, id=f"{path.relative_to(REPO)}:{svc}")
    for path in COMPOSE_FILES
    for svc, port in SERVICES.items()
]


def _healthcheck(path: Path, service: str) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw["services"][service].get("healthcheck") or {}


def _seconds(value: str) -> int:
    m = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", str(value))
    assert m and any(m.groups()), f"unparseable duration {value!r}"
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


@pytest.mark.parametrize(("path", "service", "port"), CASES)
def test_agent_healthcheck_probes_readyz(path: Path, service: str, port: int) -> None:
    hc = _healthcheck(path, service)
    assert hc, f"{service} has no healthcheck"
    assert not hc.get("disable"), f"{service} healthcheck is disabled"
    test = " ".join(hc.get("test") or [])
    assert f"localhost:{port}/readyz" in test, test
    assert "/healthz" not in test, test


@pytest.mark.parametrize(("path", "service", "port"), CASES)
def test_agent_healthcheck_start_period_covers_boot(path: Path, service: str, port: int) -> None:
    # Boot waits on Redis/Postgres; don't count it against retries.
    assert _seconds(_healthcheck(path, service).get("start_period", "0s")) >= MIN_START_PERIOD_S
