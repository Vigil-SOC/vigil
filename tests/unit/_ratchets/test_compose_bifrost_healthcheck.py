"""The Compose Bifrost healthcheck must not route its loopback probe through a proxy.

Issue #2503: busybox wget honours http_proxy/https_proxy and ignores no_proxy, so a
proxy given to the gateway caught the localhost probe, Bifrost never went healthy,
and every service gated on it stalled.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_FILES = [
    REPO / "infra" / "docker" / "docker-compose.yml",
    REPO / "clients" / "desktop" / "standalone" / "docker-compose.yml",
]
PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_bifrost_healthcheck_clears_proxy_before_wget(path: Path) -> None:
    test = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]["bifrost"]["healthcheck"]["test"]
    assert test[0] == "CMD-SHELL", test
    cmd = test[1]
    before_wget = cmd.split("wget", 1)[0]
    assert before_wget.startswith("env "), cmd
    for var in PROXY_VARS:
        assert f"-u {var} " in before_wget, f"{var} not cleared: {cmd}"
    assert "http://localhost:8080/health" in cmd, cmd
    assert """grep -q '"status":"ok"'""" in cmd, cmd
