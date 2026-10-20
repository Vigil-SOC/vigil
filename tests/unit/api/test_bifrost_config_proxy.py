"""The Bifrost config proxy: what it forwards, and what it refuses.

The two behaviours worth pinning are the path gate (a proxy that forwards
anything is a hole straight through the console's auth) and the masked-value
substitution (Bifrost accepts a write echoing its own mask and stores the mask
as the credential, after which every LLM call 401s with nothing to say why).
"""

from __future__ import annotations

import json

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
    body = {
        "vertex_key_config": {"project_id": "p", "region": "us", "auth_credentials": sa}
    }
    proxy._resolve_key_value(body, None)
    assert "value" not in body
    assert body["vertex_key_config"]["auth_credentials"] == sa


@pytest.mark.unit
def test_vertex_edit_without_credential_substitutes_the_stored_one(monkeypatch):
    # Editing project/region without re-pasting the JSON pulls the stored copy
    # into auth_credentials -- and only there, for the reason above.
    sa = '{"type": "service_account"}'
    monkeypatch.setattr(
        proxy, "get_secret", lambda ref: sa if ref == "llm_key_v1" else None
    )
    body = {"vertex_key_config": {"project_id": "p", "region": "eu"}}
    proxy._resolve_key_value(body, "v1")
    assert "value" not in body
    assert body["vertex_key_config"]["auth_credentials"] == sa


@pytest.mark.unit
def test_vertex_api_key_create_keeps_value_and_scope(monkeypatch):
    # Bifrost skips a vertex key with no vertex_key_config, API key or not, so
    # an express-mode key carries its scope beside a plain value.
    monkeypatch.setattr(proxy, "get_secret", lambda ref: None)
    body = {
        "value": "vertex-api-key",
        "vertex_key_config": {"project_id": "p", "region": "us-central1"},
    }
    proxy._resolve_key_value(body, None, "vertex")
    assert body["value"] == "vertex-api-key"
    assert body["vertex_key_config"] == {"project_id": "p", "region": "us-central1"}


@pytest.mark.unit
def test_vertex_api_key_edit_restores_value_and_scope(monkeypatch):
    # A weight-only edit retypes neither credential nor scope: both come back
    # from the stored copies, and a non-JSON credential stays an API key.
    stored = {
        "llm_key_v2": "AQ.express-key",
        "llm_key_v2_vertex_scope": '{"project_id": "p", "region": "eu"}',
    }
    monkeypatch.setattr(proxy, "get_secret", stored.get)
    body = {"weight": 2, "vertex_key_config": {}}
    proxy._resolve_key_value(body, "v2", "vertex")
    assert body["value"] == "AQ.express-key"
    assert body["vertex_key_config"] == {"project_id": "p", "region": "eu"}


@pytest.mark.unit
def test_vertex_api_key_edit_without_stored_scope_asks_for_it(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(
        proxy, "get_secret", lambda ref: "AQ.express-key" if ref == "llm_key_v3" else None
    )
    with pytest.raises(HTTPException) as exc:
        proxy._resolve_key_value({"vertex_key_config": {}}, "v3", "vertex")
    assert exc.value.status_code == 400
    assert "project id and region" in exc.value.detail


@pytest.mark.unit
def test_ollama_url_key_needs_no_credential_substitution(monkeypatch):
    # Ollama carries a URL the operator typed, not a secret we store — so a
    # models-only edit must not 400 for a missing value.
    monkeypatch.setattr(proxy, "get_secret", lambda ref: None)
    body = {"ollama_key_config": {"url": "http://localhost:11434"}, "models": ["*"]}
    proxy._resolve_key_value(body, "o1")
    assert "value" not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    "url, part",
    [
        ("http://host.docker.internal:11434.", 'has an invalid port "11434."'),
        ("http://localhost:abc", 'has an invalid port "abc"'),
        ("http://localhost:70000", 'has an invalid port "70000"'),
        ("http://localhost:0", 'has an invalid port "0"'),
        ("host:11434", "must start with http:// or https://"),
        ("http://:11434", "has no host"),
    ],
)
def test_ollama_url_that_does_not_parse_is_a_400(monkeypatch, url, part):
    from fastapi import HTTPException

    monkeypatch.setattr(proxy, "get_secret", lambda ref: "http://stored:11434")
    body = {"ollama_key_config": {"url": url}}
    with pytest.raises(HTTPException) as exc:
        proxy._resolve_key_value(body, "o1", "ollama")
    assert exc.value.status_code == 400
    assert exc.value.detail == f'The Ollama server URL "{url}" {part}'
    assert body["ollama_key_config"]["url"] == url  # rejected, not repaired


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    ["http://localhost:11434", "https://ollama.example", "env.OLLAMA_URL"],
)
def test_ollama_url_that_parses_passes_through(monkeypatch, url):
    monkeypatch.setattr(proxy, "get_secret", lambda ref: None)
    body = {"ollama_key_config": {"url": url}}
    proxy._resolve_key_value(body, "o1", "ollama")
    assert body["ollama_key_config"]["url"] == url


# --- provider base URL -------------------------------------------------------

_DOC = {
    "name": "openai",
    "network_config": {
        "default_request_timeout_in_seconds": 1800,
        "max_retries": 3,
        "insecure_skip_verify": False,
    },
    "concurrency_and_buffer_size": {"concurrency": 10, "buffer_size": 50},
    "proxy_config": None,
    "status": "active",
    "config_hash": "abc",
    "keys": [{"id": "k1", "models": ["*"]}],
}


@pytest.mark.unit
def test_base_url_is_trimmed_and_other_network_config_is_kept():
    out = proxy._merge_base_url(
        _DOC, {"network_config": {"base_url": " http://10.0.0.5:8000/v1/ "}}
    )
    net = out["network_config"]
    assert net["base_url"] == "http://10.0.0.5:8000"
    assert net["default_request_timeout_in_seconds"] == 1800
    assert net["max_retries"] == 3
    assert net["insecure_skip_verify"] is False
    # Only writable provider fields travel; read-back and keys never do.
    assert out["concurrency_and_buffer_size"] == {"concurrency": 10, "buffer_size": 50}
    assert not {"keys", "status", "config_hash"} & out.keys()


@pytest.mark.unit
def test_path_prefix_survives_and_edit_round_trips():
    first = proxy._merge_base_url(
        _DOC, {"network_config": {"base_url": "https://gw.example/openai/v1"}}
    )
    assert first["network_config"]["base_url"] == "https://gw.example/openai"
    # The panel shows the stored root; saving it back changes nothing.
    again = proxy._merge_base_url(
        {**_DOC, "network_config": first["network_config"]},
        {"network_config": {"base_url": first["network_config"]["base_url"]}},
    )
    assert again["network_config"] == first["network_config"]


@pytest.mark.unit
def test_saving_a_host_allows_private_network_and_clearing_drops_both():
    saved = proxy._merge_base_url(
        _DOC, {"network_config": {"base_url": "http://192.168.1.20:8000"}}
    )
    assert saved["network_config"]["allow_private_network"] is True

    cleared = proxy._merge_base_url(
        {**_DOC, "network_config": saved["network_config"]},
        {"network_config": {"base_url": ""}},
    )
    assert "base_url" not in cleared["network_config"]
    assert "allow_private_network" not in cleared["network_config"]
    assert cleared["network_config"]["max_retries"] == 3


@pytest.mark.unit
def test_clearing_nothing_leaves_a_seeded_private_flag_alone():
    seeded = {**_DOC, "network_config": {**_DOC["network_config"], "allow_private_network": True}}
    out = proxy._merge_base_url(seeded, {"network_config": {"base_url": ""}})
    assert out["network_config"]["allow_private_network"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "ftp://host/v1",
        "http://user:pw@host/v1",
        "http://host/v1#frag",
        "no-scheme",
        "http://host:8000./v1",
        7,
    ],
)
def test_bad_base_url_is_a_400(bad):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        proxy._merge_base_url(_DOC, {"network_config": {"base_url": bad}})
    assert exc.value.status_code == 400


class _FakeGateway:
    """Bifrost's provider document, served through ``httpx.AsyncClient``."""

    def __init__(self, doc):
        self.doc = doc
        self.writes = []

    def transport(self):
        import httpx

        def handle(request):
            if request.method == "GET":
                return httpx.Response(200, json=self.doc)
            self.writes.append(json.loads(request.content) if request.content else None)
            return httpx.Response(200, json={"ok": True})

        return httpx.MockTransport(handle)


def _client(monkeypatch, gateway):
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    real = httpx.AsyncClient
    monkeypatch.setattr(
        proxy.httpx,
        "AsyncClient",
        lambda **kw: real(transport=gateway.transport(), **kw),
    )
    monkeypatch.setattr(proxy, "_require_settings_admin", lambda user: None)
    app = FastAPI()
    app.include_router(proxy.router, prefix="/api/bifrost")
    app.dependency_overrides[proxy.get_current_active_user] = lambda: object()
    return TestClient(app)


@pytest.mark.unit
def test_put_with_a_bad_url_is_refused_before_bifrost_sees_it(monkeypatch):
    gw = _FakeGateway(_DOC)
    res = _client(monkeypatch, gw).put(
        "/api/bifrost/providers/openai",
        json={"network_config": {"base_url": "file:///etc/passwd"}},
    )
    assert res.status_code == 400
    assert gw.writes == []


@pytest.mark.unit
def test_put_forwards_the_merged_document(monkeypatch):
    gw = _FakeGateway(_DOC)
    res = _client(monkeypatch, gw).put(
        "/api/bifrost/providers/openai",
        json={"network_config": {"base_url": "http://10.0.0.5:8000/v1"}},
    )
    assert res.status_code == 200
    sent = gw.writes[0]
    assert sent["network_config"]["base_url"] == "http://10.0.0.5:8000"
    assert sent["network_config"]["allow_private_network"] is True
    assert sent["network_config"]["max_retries"] == 3
    assert "keys" not in sent


@pytest.mark.unit
def test_put_without_a_base_url_passes_through_untouched(monkeypatch):
    gw = _FakeGateway(_DOC)
    body = {"network_config": {"max_retries": 1}}
    res = _client(monkeypatch, gw).put("/api/bifrost/providers/openai", json=body)
    assert res.status_code == 200
    assert gw.writes == [body]


@pytest.mark.unit
def test_ollama_key_with_a_bad_url_is_refused_before_bifrost_sees_it(monkeypatch):
    monkeypatch.setattr(proxy, "get_secret", lambda ref: None)
    gw = _FakeGateway(_DOC)
    res = _client(monkeypatch, gw).put(
        "/api/bifrost/providers/ollama/keys/o1",
        json={"ollama_key_config": {"url": "http://host.docker.internal:11434."}},
    )
    assert res.status_code == 400
    assert 'invalid port "11434."' in res.json()["detail"]
    assert gw.writes == []


# --- custom (OpenAI-compatible) providers ------------------------------------

_CUSTOM_CREATE = {
    "provider": "lan-vllm",
    "network_config": {"base_url": "http://10.0.0.5:8000/v1"},
    "custom_provider_config": {"base_provider_type": "openai", "is_key_less": True},
}


def _mirrored(monkeypatch):
    seen = []

    async def sync(provider):
        seen.append(provider)

    monkeypatch.setattr(proxy.mirror, "sync_provider", sync)
    return seen


@pytest.mark.unit
def test_create_keeps_the_custom_block_normalizes_the_host_and_mirrors(monkeypatch):
    gw, seen = _FakeGateway({}), _mirrored(monkeypatch)
    res = _client(monkeypatch, gw).post("/api/bifrost/providers", json=_CUSTOM_CREATE)
    assert res.status_code == 200
    sent = gw.writes[0]
    assert sent["provider"] == "lan-vllm"
    assert sent["network_config"] == {
        "base_url": "http://10.0.0.5:8000",
        "allow_private_network": True,
    }
    assert sent["custom_provider_config"] == _CUSTOM_CREATE["custom_provider_config"]
    # A key-less provider has no key write to create its row, so the create does.
    assert seen == ["lan-vllm"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "patch",
    [
        {"provider": "openai"},  # a built-in's name
        {"provider": "Lan_VLLM"},
        {"provider": "-x"},
        {"network_config": {}},  # no host to fall back to
        {"network_config": {"base_url": ""}},
        {"custom_provider_config": {"base_provider_type": "anthropic"}},
    ],
)
def test_a_malformed_custom_provider_never_reaches_bifrost(monkeypatch, patch):
    gw, seen = _FakeGateway({}), _mirrored(monkeypatch)
    res = _client(monkeypatch, gw).post(
        "/api/bifrost/providers", json={**_CUSTOM_CREATE, **patch}
    )
    assert res.status_code == 400
    assert gw.writes == [] and seen == []


@pytest.mark.unit
def test_a_write_cannot_turn_an_existing_provider_custom(monkeypatch):
    gw = _FakeGateway(_DOC)
    res = _client(monkeypatch, gw).put(
        "/api/bifrost/providers/openai",
        json={"custom_provider_config": {"base_provider_type": "openai"}},
    )
    assert res.status_code == 400
    assert gw.writes == []


@pytest.mark.unit
def test_editing_a_custom_provider_url_keeps_its_custom_block(monkeypatch):
    doc = {
        **_DOC,
        "name": "lan-vllm",
        "network_config": {"base_url": "http://10.0.0.5:8000", "allow_private_network": True},
        "custom_provider_config": {"base_provider_type": "openai", "is_key_less": True},
    }
    gw = _FakeGateway(doc)
    res = _client(monkeypatch, gw).put(
        "/api/bifrost/providers/lan-vllm",
        json={"network_config": {"base_url": "http://10.0.0.9:8000/v1"}},
    )
    assert res.status_code == 200
    sent = gw.writes[0]
    assert sent["network_config"]["base_url"] == "http://10.0.0.9:8000"
    assert sent["custom_provider_config"] == doc["custom_provider_config"]


@pytest.mark.unit
def test_deleting_a_provider_retires_its_mirror_row_and_only_its_own(monkeypatch):
    gw, seen = _FakeGateway({}), _mirrored(monkeypatch)
    client = _client(monkeypatch, gw)
    # The fake gateway answers every non-GET with 200.
    assert client.delete("/api/bifrost/providers/lan-vllm").status_code == 200
    assert client.delete("/api/bifrost/providers/lab-gw").status_code == 200
    assert seen == ["lan-vllm", "lab-gw"]


@pytest.mark.unit
@pytest.mark.parametrize("blank", ["  ", "/v1"])
def test_a_host_that_normalizes_to_nothing_is_not_a_custom_provider_host(
    monkeypatch, blank
):
    gw = _FakeGateway({})
    body = {**_CUSTOM_CREATE, "network_config": {"base_url": blank}}
    res = _client(monkeypatch, gw).post("/api/bifrost/providers", json=body)
    assert res.status_code == 400
    assert gw.writes == []


@pytest.mark.unit
def test_a_custom_provider_cannot_lose_its_host_or_its_custom_block(monkeypatch):
    doc = {
        **_DOC,
        "name": "lan-vllm",
        "network_config": {"base_url": "http://10.0.0.5:8000"},
        "custom_provider_config": {"base_provider_type": "openai"},
    }
    gw = _FakeGateway(doc)
    client = _client(monkeypatch, gw)
    cleared = client.put(
        "/api/bifrost/providers/lan-vllm", json={"network_config": {"base_url": ""}}
    )
    unset = client.put(
        "/api/bifrost/providers/lan-vllm", json={"custom_provider_config": None}
    )
    assert cleared.status_code == unset.status_code == 400
    assert gw.writes == []
