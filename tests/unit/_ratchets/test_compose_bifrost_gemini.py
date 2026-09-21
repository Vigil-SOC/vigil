"""A machine holding a Google API key gets a Gemini route from `compose up`.

Bifrost's `vertex` provider is seeded, but its shape wants a project, a region
and a service-account JSON, and the project-scoped path refuses a plain key
outright ("API keys are not supported by this API"). `gemini` -- Google AI
Studio -- is the Bifrost provider that takes a bare key, and everything else
here already treats it as first class: `registry.py` prices it,
`url_safety.py` allow-lists its host, `bifrostApi.ts` offers it. Only the seed
was missing, and eight live end-to-end runs stalled on that.

Two details are load-bearing and easy to lose in a reformat, so they are
pinned: the key's `models` must be `["*"]`, because a key pushed with `[]`
routes nothing (`no keys found that support model`), and the container needs
the variable in its own environment -- the seed reads `env.GEMINI_API_KEY`
from inside Bifrost, not from the host shell.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPO / "infra" / "docker" / "docker-compose.yml"
BIFROST_CONFIG = REPO / "infra" / "docker" / "bifrost" / "config.json"


@pytest.fixture(scope="module")
def gemini_provider() -> dict:
    providers = json.loads(BIFROST_CONFIG.read_text())["providers"]
    assert "gemini" in providers, f"seeded providers: {sorted(providers)}"
    return providers["gemini"]


@pytest.fixture(scope="module")
def bifrost_environment() -> list[str]:
    compose = yaml.safe_load(COMPOSE_PATH.read_text())
    return compose["services"]["bifrost"]["environment"]


def test_the_seeded_key_reads_the_environment(gemini_provider):
    keys = gemini_provider["keys"]
    assert len(keys) == 1, keys
    assert keys[0]["value"] == "env.GEMINI_API_KEY"


def test_the_seeded_key_routes_every_model(gemini_provider):
    """`[]` is what `_upsert_provider_key` sends on first push, and it routes nothing."""
    assert gemini_provider["keys"][0]["models"] == ["*"]


def test_the_container_is_given_the_variable(bifrost_environment):
    entry = [e for e in bifrost_environment if e.startswith("GEMINI_API_KEY")]
    assert entry, bifrost_environment
    # `:-` so an unset key is an empty string Bifrost tolerates, as the
    # anthropic and openai seeds already do, rather than a compose warning.
    assert entry[0] == "GEMINI_API_KEY=${GEMINI_API_KEY:-}"


def test_vertex_is_left_alone(gemini_provider):
    """Installs holding a service account keep the provider that wants one."""
    providers = json.loads(BIFROST_CONFIG.read_text())["providers"]
    assert "vertex" in providers
    assert "vertex_key_config" in providers["vertex"]["keys"][0]


def test_the_default_model_floor_is_one_a_new_key_can_call():
    """Google AI Studio 404s `gemini-2.5-flash` for keys issued after its retirement."""
    from core.llm.bifrost.admin import _CATALOG_DEFAULT_PREFERENCE

    assert _CATALOG_DEFAULT_PREFERENCE["gemini"][0] == "gemini-flash-latest"
    assert "gemini-2.5-flash" not in _CATALOG_DEFAULT_PREFERENCE["gemini"]
