"""The Bifrost config proxy: what it forwards, and what it refuses.

The two behaviours worth pinning are the path gate (a proxy that forwards
anything is a hole straight through the console's auth) and the masked-value
substitution (Bifrost accepts a write echoing its own mask and stores the mask
as the credential, after which every LLM call 401s with nothing to say why).
"""

from __future__ import annotations

import pytest

from services.api.routers import bifrost_config as proxy


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "providers",
        "providers/anthropic",
        "providers/anthropic/keys",
        "providers/anthropic/keys/7eeaad6a-529a-490c-a384-b6fbaf3e02f4",
        "keys",
        "models",
        "models/base",
        "models/details",
        "models/parameters",
        "governance/virtual-keys",
        "governance/budgets",
        "governance/rate-limits",
        "governance/budgets/some-id",
    ],
)
def test_proxied_paths_are_allowed(path):
    assert proxy._ALLOWED_PATHS.match(path)


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "",
        "config",  # gateway internals — not a settings surface
        "plugins",
        "logs",  # has its own read-side client in core.llm.bifrost.costs
        "governance/teams",  # out of scope: no multi-tenant hierarchy in Vigil
        "governance/customers",
        "providers/anthropic/keys/abc/extra",
        "../config",
        "providers/a/../../config",
    ],
)
def test_everything_else_is_refused(path):
    assert not proxy._ALLOWED_PATHS.match(path)


@pytest.mark.unit
def test_read_back_values_are_recognised_as_masked():
    # Bifrost's own read shapes: the wrapper object and the starred string.
    assert proxy._is_masked({"value": "sk-a****key", "from_env": False})
    assert proxy._is_masked("sk-a************CwAA")
    assert not proxy._is_masked("sk-ant-a-real-looking-credential")
    assert not proxy._is_masked("")


@pytest.mark.unit
def test_masked_write_substitutes_the_stored_secret(monkeypatch):
    monkeypatch.setattr(
        proxy, "get_secret", lambda ref: "sk-real" if ref == "llm_key_k1" else None
    )
    body = {"value": "sk-a****key", "models": ["claude-sonnet-5"]}
    proxy._resolve_key_value(body, "k1")
    assert body["value"] == "sk-real"
    assert body["models"] == ["claude-sonnet-5"]


@pytest.mark.unit
def test_new_secret_is_left_alone(monkeypatch):
    monkeypatch.setattr(proxy, "get_secret", lambda ref: "sk-stored")
    body = {"value": "sk-brand-new"}
    proxy._resolve_key_value(body, "k1")
    assert body["value"] == "sk-brand-new"


@pytest.mark.unit
def test_editing_a_key_we_hold_no_secret_for_is_a_400(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(proxy, "get_secret", lambda ref: None)
    with pytest.raises(HTTPException) as exc:
        proxy._resolve_key_value({"models": ["x"]}, "unknown-key")
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_vertex_service_account_is_not_mirrored_to_value(monkeypatch):
    # The JSON is used verbatim and stays in auth_credentials alone. Bifrost
    # picks a vertex key's auth mode by whether `value` is populated, so
    # copying it there made the gateway authenticate as an API key and fail
    # with "API keys are not supported by this API" -- a 401 that names
    # nothing of the real cause.
    monkeypatch.setattr(proxy, "get_secret", lambda ref: None)
    sa = '{"type": "service_account", "project_id": "p"}'
    body = {"vertex_key_config": {"project_id": "p", "region": "us", "auth_credentials": sa}}
    proxy._resolve_key_value(body, None)
    assert "value" not in body
    assert body["vertex_key_config"]["auth_credentials"] == sa


@pytest.mark.unit
def test_vertex_edit_without_credential_substitutes_the_stored_one(monkeypatch):
    # Editing project/region without re-pasting the JSON pulls the stored copy
    # into auth_credentials -- and only there, for the reason above.
    monkeypatch.setattr(
        proxy, "get_secret", lambda ref: "stored-sa" if ref == "llm_key_v1" else None
    )
    body = {"vertex_key_config": {"project_id": "p", "region": "eu"}}
    proxy._resolve_key_value(body, "v1")
    assert "value" not in body
    assert body["vertex_key_config"]["auth_credentials"] == "stored-sa"


@pytest.mark.unit
def test_vertex_api_key_takes_the_plain_value_path(monkeypatch):
    # API-key vertex sends no vertex_key_config, so it is handled like any other
    # bare credential.
    monkeypatch.setattr(proxy, "get_secret", lambda ref: "stored")
    body = {"value": "vertex-api-key"}
    proxy._resolve_key_value(body, "v2")
    assert body["value"] == "vertex-api-key"


@pytest.mark.unit
def test_ollama_url_key_needs_no_credential_substitution(monkeypatch):
    # Ollama carries a URL the operator typed, not a secret we store — so a
    # models-only edit must not 400 for a missing value.
    monkeypatch.setattr(proxy, "get_secret", lambda ref: None)
    body = {"ollama_key_config": {"url": "http://localhost:11434"}, "models": ["*"]}
    proxy._resolve_key_value(body, "o1")
    assert "value" not in body
