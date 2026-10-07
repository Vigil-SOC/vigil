"""The Compose llm-worker must probe ARQ liveness, as the Helm chart does.

Issue #1589: the worker serves no HTTP, so the backend image's curl HEALTHCHECK
was disabled outright and a dead or wedged worker was invisible under Compose.
`arq --check` asks Redis whether the worker's health key is still being written.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "infra" / "docker" / "docker-compose.yml"
HELM_TEMPLATE = REPO / "infra" / "helm" / "vigil" / "templates" / "llm-worker-deployment.yaml"
WORKER_SETTINGS = "services.worker.jobs.WorkerSettings"


def _healthcheck() -> dict:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return raw["services"]["llm-worker"].get("healthcheck") or {}


def test_llm_worker_healthcheck_is_not_disabled() -> None:
    hc = _healthcheck()
    assert hc, "llm-worker has no healthcheck"
    assert not hc.get("disable"), "llm-worker healthcheck is disabled"


def test_llm_worker_healthcheck_runs_arq_check_like_helm() -> None:
    test = _healthcheck().get("test")
    assert test == ["CMD", "arq", "--check", WORKER_SETTINGS], test
    assert WORKER_SETTINGS in HELM_TEMPLATE.read_text(encoding="utf-8")


def test_llm_worker_healthcheck_allows_on_startup_before_first_key() -> None:
    # on_startup can take ~300 s before the first health key is written.
    assert _healthcheck().get("start_period") in ("300s", "5m")
