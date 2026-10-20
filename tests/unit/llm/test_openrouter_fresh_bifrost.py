"""OpenRouter added to a fresh Bifrost routes the models assigned to it (#2290).

The gateway here is a stateful fake that keeps the one behaviour of Bifrost
v2.2.1 the bug turned on: a key lists and routes only what its ``models``
allow-list names, ``*`` names everything, and an empty list names nothing --
including through ``/api/models/details``, which is OpenRouter's only catalogue.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from core.llm.bifrost import admin as ba
from core.llm.bifrost import mirror
from core.llm.providers import registry as model_registry
from core.llm.target import can_serve

pytestmark = pytest.mark.unit

HAIKU = "anthropic/claude-haiku-5.5"
UPSTREAM = {
    "openrouter": [HAIKU, "google/gemini-3-flash", "openai/gpt-5-mini"],
    "anthropic": ["claude-sonnet-4-6"],
}


# What the host behind a custom provider serves. The gateway's datasheet knows
# nothing of it, so only /v1/models can say.
CUSTOM_UPSTREAM = ["qwen2.5:3b"]


class FakeBifrost:
    """A fresh Bifrost: the providers ``config.json`` seeds, none of them OpenRouter."""

    def __init__(self) -> None:
        self.providers: Dict[str, List[Dict[str, Any]]] = {"anthropic": []}
        self.network: Dict[str, Dict[str, Any]] = {}
        # custom providers: name -> custom_provider_config
        self.custom: Dict[str, Dict[str, Any]] = {}
        self._ids = itertools.count(1)

    def allowed(self, provider: str) -> List[str]:
        out: List[str] = []
        for key in self.providers.get(provider) or []:
            models = key.get("models") or []
            for mid in UPSTREAM.get(provider, []):
                if ("*" in models or mid in models) and mid not in out:
                    out.append(mid)
        return out

    def routes(self, provider: str, model: str) -> bool:
        return model in self.allowed(provider)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = urlparse(str(request.url)).path.strip("/").split("/")
        body = json.loads(request.content) if request.content else None
        method = request.method
        if path == ["api", "providers"]:
            if method == "GET":
                listed = [
                    {"name": n, **({"custom_provider_config": self.custom[n]} if n in self.custom else {})}
                    for n in self.providers
                ]
                return httpx.Response(200, json={"providers": listed})
            name = body["provider"]
            if name in self.providers:
                return httpx.Response(409, json={"error": "exists"})
            self.providers[name] = []
            self.network[name] = body.get("network_config") or {}
            return httpx.Response(200, json={"name": name})
        if path[:2] == ["api", "providers"] and len(path) >= 3:
            name = path[2]
            if name not in self.providers:
                return httpx.Response(404, json={"error": "Provider not found"})
            if len(path) == 3:
                return httpx.Response(
                    200,
                    json={"name": name, "network_config": self.network.get(name, {})},
                )
            keys = self.providers[name]
            if method == "GET":
                shown = [{**k, "value": "sk-****", "status": "success"} for k in keys]
                return httpx.Response(200, json={"keys": shown, "total": len(shown)})
            if method == "POST":
                key = {**body, "id": f"key-{next(self._ids)}"}
                keys.append(key)
                return httpx.Response(200, json={**key, "status": "success"})
            if method == "PUT":
                for i, key in enumerate(keys):
                    if key["id"] == path[4]:
                        keys[i] = {**body, "id": key["id"]}
                        return httpx.Response(
                            200, json={**keys[i], "status": "success"}
                        )
                return httpx.Response(404, json={"error": "Key not found"})
        if path == ["v1", "models"]:
            provider = parse_qs(urlparse(str(request.url)).query)["provider"][0]
            data = [{"id": f"{provider}/{m}"} for m in CUSTOM_UPSTREAM]
            return httpx.Response(200, json={"data": data})
        if path == ["api", "models", "details"]:
            provider = parse_qs(urlparse(str(request.url)).query)["provider"][0]
            models = [
                {"name": mid, "provider": provider, "max_output_tokens": 8192}
                for mid in self.allowed(provider)
            ]
            return httpx.Response(200, json={"models": models, "total": len(models)})
        return httpx.Response(404, json={"error": f"unrouted {method} {path}"})


class _Row:
    def __init__(self, provider_id: str, provider_type: str, api_key_ref=None):
        self.provider_id = provider_id
        self.provider_type = provider_type
        self.base_url = None
        self.api_key_ref = api_key_ref
        self.config: Dict[str, Any] = {}
        self.is_active = True
        self.is_default = True


class _Scope:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        rows = self.rows

        class _Query:
            def filter(self, *_):
                return self

            def all(self):
                return rows

        class _Session:
            def query(self, _model):
                return _Query()

        return _Session()

    def __exit__(self, *_):
        return False


@pytest.fixture
def bifrost(monkeypatch):
    fake = FakeBifrost()
    transport = httpx.MockTransport(fake.handle)
    real_client, real_async = httpx.Client, httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real_async(transport=transport, **kw)
    )
    model_registry._MODEL_LIST_CACHE.clear()
    model_registry._LIVE_CATALOGUES.clear()
    yield fake
    model_registry._MODEL_LIST_CACHE.clear()
    model_registry._LIVE_CATALOGUES.clear()


@pytest.fixture
def mirrored(monkeypatch) -> Dict[str, str]:
    """Mirror rows the reconcile would write, by provider type."""
    rows: Dict[str, str] = {}

    def upsert(provider: str, default_model: str, servable: Optional[set] = None):
        rows[provider] = default_model

    monkeypatch.setattr(mirror, "_upsert_row", upsert)
    monkeypatch.setattr(mirror, "_deactivate_row", lambda provider: None)
    return rows


def _sync(monkeypatch, rows, secrets):
    db = type(
        "DB", (), {"_engine": object(), "session_scope": lambda self: _Scope(rows)}
    )()
    monkeypatch.setattr("core.storage.connection.get_db_manager", lambda: db)
    monkeypatch.setattr("core.secrets_manager.get_secret", secrets.get)
    return asyncio.run(ba._do_sync_all_provider_models())


def test_a_key_added_to_a_fresh_bifrost_leaves_its_assigned_model_routable(
    bifrost, mirrored, monkeypatch
):
    # POST /api/llm/providers: the secret is stored, then pushed live.
    assert ba.push_provider_key("openrouter", "sk-or-real") is True
    assert bifrost.providers["openrouter"][0]["models"] == ["*"]
    assert bifrost.network["openrouter"]["default_request_timeout_in_seconds"] == 1800

    owned = _Row("openrouter", "openrouter", api_key_ref="llm_provider_or_key")
    result = _sync(monkeypatch, [owned], {"llm_provider_or_key": "sk-or-real"})

    # Assigning the model: the same check PUT /api/ai/config makes.
    assert HAIKU in model_registry.catalogue_of("openrouter")
    assert can_serve(owned, HAIKU)
    assert bifrost.routes("openrouter", HAIKU)
    # Not narrowed to a snapshot of the catalogue the wildcard produced.
    assert bifrost.providers["openrouter"][0]["models"] == ["*"]
    assert result["bifrost"]["openrouter"] is True
    assert "openrouter" in mirrored


def test_a_key_added_in_settings_is_mirrored(bifrost, mirrored):
    # Settings > AI models > Add provider, then Add key: the console's proxy
    # forwards both writes as is, then mirrors the provider.
    httpx.Client().post(
        "http://bifrost:8080/api/providers", json={"provider": "openrouter"}
    )
    httpx.Client().post(
        "http://bifrost:8080/api/providers/openrouter/keys",
        json={"name": "openrouter-key", "value": "sk-or-real", "models": ["*"]},
    )

    asyncio.run(mirror.sync_provider("openrouter"))

    # Floored to a model that calls tools, not the first "flash" id listed.
    assert mirrored["openrouter"] == HAIKU


def test_a_provider_added_in_settings_takes_a_key_from_the_api(
    bifrost, mirrored, monkeypatch
):
    # The rehearsal's order: the provider through Settings with no key, the key
    # through POST /api/llm/providers.
    bifrost.providers["openrouter"] = []

    assert ba.push_provider_key("openrouter", "sk-or-real") is True

    assert bifrost.routes("openrouter", HAIKU)
    _sync(monkeypatch, [], {})
    assert "openrouter" in mirrored


def test_a_sync_opens_a_key_left_with_no_models(bifrost, mirrored, monkeypatch):
    # What the old push left behind on an install that took the key before this.
    bifrost.providers["openrouter"] = [
        {"id": "key-0", "name": "default-openrouter-key", "models": []}
    ]
    owned = _Row("openrouter", "openrouter", api_key_ref="ref")

    _sync(monkeypatch, [owned], {"ref": "sk-or-real"})

    assert bifrost.providers["openrouter"][0]["models"] == ["*"]
    assert bifrost.providers["openrouter"][0]["value"] == "sk-or-real"
    # Opened after this pass listed it, so the next pass reads the catalogue.
    _sync(monkeypatch, [owned], {"ref": "sk-or-real"})
    assert can_serve(owned, HAIKU)


def test_a_sync_keeps_an_allow_list_the_operator_narrowed(
    bifrost, mirrored, monkeypatch
):
    bifrost.providers["openrouter"] = [
        {"id": "key-0", "name": "default-openrouter-key", "models": [HAIKU]}
    ]
    owned = _Row("openrouter", "openrouter", api_key_ref="ref")

    _sync(monkeypatch, [owned], {"ref": "sk-or-real"})

    assert bifrost.providers["openrouter"][0]["models"] == [HAIKU]
    assert model_registry.catalogue_of("openrouter") == [HAIKU]


def test_a_custom_provider_lists_through_the_gateway_and_is_never_rewritten(
    bifrost, mirrored, monkeypatch
):
    bifrost.providers["lan-vllm"] = [{"id": "key-0", "name": "k", "models": ["*"]}]
    bifrost.custom["lan-vllm"] = {"base_provider_type": "openai"}
    row = _Row("bifrost-lan-vllm", "lan-vllm")
    row.config = {"managed_by": "bifrost"}
    host_writes: List[Any] = []
    monkeypatch.setattr(ba, "sync_provider_base_url", lambda *a: host_writes.append(a))

    result = _sync(monkeypatch, [row], {})

    assert mirrored["lan-vllm"] == "qwen2.5:3b"
    # The picker gets the models its own host serves, not an empty list.
    assert model_registry._MODEL_LIST_CACHE["bifrost-lan-vllm"] == ["qwen2.5:3b"]
    assert "bifrost-lan-vllm" in model_registry._LIVE_CATALOGUES
    # Its key keeps the allow-list it has, and its host is not pushed by type.
    assert bifrost.providers["lan-vllm"][0]["models"] == ["*"]
    assert result["bifrost"]["lan-vllm"] is True
    assert host_writes == []
