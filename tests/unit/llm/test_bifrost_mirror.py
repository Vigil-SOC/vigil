"""Unit tests for the routability predicate in core.llm.bifrost.mirror.

Bifrost >= 1.6 reports a credential as ``{"value", "ref", "type"}``; the old
shape was ``{"env_var", "from_env"}``. The guard must recognise an unresolved
reference in either shape and must NOT mistake a resolved env credential
(masked, non-empty value) for a placeholder.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.llm.bifrost.mirror import (  # noqa: E402
    _cred_from_env,
    key_health,
    key_is_routable,
)

pytestmark = pytest.mark.unit

UNRESOLVED_ENV = {"value": "", "ref": "env.ANTHROPIC_API_KEY", "type": "env"}
RESOLVED_ENV = {"value": "sk-***abcd", "ref": "env.ANTHROPIC_API_KEY", "type": "env"}
LITERAL = {"value": "sk-***abcd", "ref": "", "type": ""}


@pytest.mark.parametrize(
    "cred, expected",
    [
        (UNRESOLVED_ENV, True),
        (RESOLVED_ENV, False),
        (LITERAL, False),
        ({"value": "", "ref": "vault.secret/x", "type": "vault"}, True),
        ({"value": "", "ref": "env.X"}, True),  # ref only, no type
        ({"env_var": "ANTHROPIC_API_KEY", "from_env": True}, True),  # legacy
        # legacy marker but resolved: same "empty value" rule as the new shape
        ({"env_var": "ANTHROPIC_API_KEY", "from_env": True, "value": "sk-***"}, False),
        ({"value": ""}, False),  # empty but not a reference: not our call
    ],
)
def test_cred_from_env_value_shapes(cred, expected):
    assert _cred_from_env({"value": cred}) is expected


def test_cred_from_env_vertex_auth_credentials():
    key = {
        "value": RESOLVED_ENV,
        "vertex_key_config": {
            "auth_credentials": {
                "value": "",
                "ref": "env.GOOGLE_APPLICATION_CREDENTIALS",
                "type": "env",
            }
        },
    }
    assert _cred_from_env(key) is True
    key["vertex_key_config"]["auth_credentials"]["value"] = "{***}"
    assert _cred_from_env(key) is False


def test_string_value_is_not_a_reference():
    assert _cred_from_env({"value": "sk-literal"}) is False


def test_unresolved_seed_is_not_routable_without_status():
    assert key_is_routable({"value": UNRESOLVED_ENV}, "anthropic") is False
    assert key_is_routable({"value": UNRESOLVED_ENV, "status": "unknown"}) is False


def test_resolved_and_literal_keys_are_routable_without_status():
    assert key_is_routable({"value": RESOLVED_ENV}, "anthropic") is True
    assert key_is_routable({"value": LITERAL}, "openai") is True


def test_unverifiable_vertex_with_unresolved_creds_is_not_routable():
    key = {
        "value": UNRESOLVED_ENV,
        "status": "list_models_failed",
        "vertex_key_config": {
            "auth_credentials": {"value": "", "ref": "env.GAC", "type": "env"}
        },
    }
    assert key_is_routable(key, "vertex") is False
    key["value"] = RESOLVED_ENV
    key["vertex_key_config"]["auth_credentials"]["value"] = "{***}"
    assert key_is_routable(key, "vertex") is True


@pytest.mark.parametrize(
    "key",
    [
        {"value": UNRESOLVED_ENV},
        {"value": {"env_var": "X", "from_env": True}},
        {"ollama_key_config": {"url": RESOLVED_ENV}},
    ],
)
def test_success_status_is_routable_regardless_of_shape(key):
    assert key_is_routable({**key, "status": "success"}) is True


def test_disabled_key_is_never_routable():
    assert (
        key_is_routable({"value": LITERAL, "status": "success", "enabled": False})
        is False
    )


def test_unresolved_seed_is_a_placeholder_whatever_bifrost_tried():
    """The console hides a placeholder; every other label it shows and badges,
    so an operator's real key -- env-backed or typed, checked or refused --
    must never read as one."""
    assert key_health({"value": UNRESOLVED_ENV}, "anthropic") == "placeholder"
    assert key_health({"value": UNRESOLVED_ENV, "status": "unknown"}) == "placeholder"
    # vertex's seed: Bifrost tried the listing and could not
    assert (
        key_health({"value": UNRESOLVED_ENV, "status": "list_models_failed"}, "vertex")
        == "placeholder"
    )
    assert key_health({"value": RESOLVED_ENV}, "anthropic") == "unverified"
    assert key_health({"value": LITERAL, "status": "unknown"}) == "unverified"
    assert (
        key_health({"value": RESOLVED_ENV, "status": "list_models_failed"})
        == "rejected"
    )
    # a resolved URL nothing answers at is a key that was refused, not a placeholder
    ollama = {"value": {"value": ""}, "ollama_key_config": {"url": RESOLVED_ENV}}
    assert (
        key_health({**ollama, "status": "list_models_failed"}, "ollama") == "rejected"
    )
    assert key_health({"value": UNRESOLVED_ENV, "enabled": False}) == "disabled"


# --- custom providers --------------------------------------------------------


def _custom(name, *, key_less=False):
    return {
        "name": name,
        "custom_provider_config": {"base_provider_type": "openai", "is_key_less": key_less},
    }


class _Gateway:
    """``mirror._get`` over a dict of path -> payload; a missing path is a 404."""

    def __init__(self, routes):
        self.routes = routes

    async def get(self, path):
        import httpx

        if path not in self.routes:
            req = httpx.Request("GET", f"http://bifrost/api/{path}")
            httpx.Response(404, request=req).raise_for_status()
        return self.routes[path]


def _applied(monkeypatch, routes):
    from core.llm.bifrost import mirror

    calls = []

    async def fake_apply(provider, *, routable):
        calls.append((provider, routable))

    monkeypatch.setattr(mirror, "_get", _Gateway(routes).get)
    monkeypatch.setattr(mirror, "_apply", fake_apply)
    return calls


KEY_OK = {"id": "k1", "status": "success", "value": LITERAL}


@pytest.mark.asyncio
async def test_a_key_less_provider_routes_with_no_key(monkeypatch):
    from core.llm.bifrost import mirror

    calls = _applied(
        monkeypatch,
        {"providers/lab-gw": _custom("lab-gw", key_less=True), "providers/lab-gw/keys": {"keys": None}},
    )
    await mirror.sync_provider("lab-gw")
    assert calls == [("lab-gw", True)]


@pytest.mark.asyncio
async def test_a_custom_provider_without_a_key_does_not_route(monkeypatch):
    from core.llm.bifrost import mirror

    calls = _applied(
        monkeypatch,
        {"providers/lan-vllm": _custom("lan-vllm"), "providers/lan-vllm/keys": {"keys": None}},
    )
    await mirror.sync_provider("lan-vllm")
    assert calls == [("lan-vllm", False)]


@pytest.mark.asyncio
async def test_a_provider_deleted_in_bifrost_retires_its_row(monkeypatch):
    from core.llm.bifrost import mirror

    calls = _applied(monkeypatch, {})
    await mirror.sync_provider("lan-vllm")
    assert calls == [("lan-vllm", False)]


@pytest.mark.asyncio
async def test_two_custom_providers_are_judged_separately(monkeypatch):
    from core.llm.bifrost import mirror

    routes = {
        "providers": {"providers": [_custom("lan-vllm"), _custom("lab-gw", key_less=True)]},
        "providers/lan-vllm/keys": {"keys": [KEY_OK]},
        "providers/lab-gw/keys": {"keys": None},
    }
    calls = _applied(monkeypatch, routes)
    monkeypatch.setattr(mirror, "_get", _Gateway(routes).get)

    assert await mirror.reconcile_all() == {"lan-vllm": True, "lab-gw": True}
    assert sorted(calls) == [("lab-gw", True), ("lan-vllm", True)]

    verdicts = await mirror.routability()
    assert verdicts["providers"] == {"lan-vllm": True, "lab-gw": True}
    assert verdicts["keys"]["k1"]["provider"] == "lan-vllm"
