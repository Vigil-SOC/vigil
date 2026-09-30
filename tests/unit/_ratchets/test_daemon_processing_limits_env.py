"""The daemon's processing limits reach it from both Compose and Helm.

soc-daemon has an explicit environment list, not an env_file, so a limit
that is missing from it is silently fixed at its default in Compose.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from core.config import Settings

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "infra" / "docker" / "docker-compose.yml"
HELM_VALUES_PATH = REPO / "infra" / "helm" / "vigil" / "values.yaml"

LIMITS = ("DAEMON_HANDOFF_QUEUE_MAXSIZE", "DAEMON_ENRICH_MAX_INFLIGHT")


def _default(key: str) -> str:
    return str(Settings.model_fields[key.lower()].default)


@pytest.mark.parametrize("key", LIMITS)
def test_compose_forwards_the_limit_with_the_settings_default(key):
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    env = raw["services"]["soc-daemon"]["environment"]
    assert env.get(key) == f"${{{key}:-{_default(key)}}}"


@pytest.mark.parametrize("key", LIMITS)
def test_helm_lists_the_limit_with_the_settings_default(key):
    raw = yaml.safe_load(HELM_VALUES_PATH.read_text(encoding="utf-8"))
    assert raw["config"].get(key) == _default(key)
