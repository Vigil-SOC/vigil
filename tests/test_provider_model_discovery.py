"""Unit tests for services.provider_model_discovery (GH #139).

Focus: per-provider fetch behavior, retry, cache TTL, fallback on
transient error. httpx is monkeypatched so tests never hit the network.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from services import provider_model_discovery as discovery  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_cache():
    """Isolation: drop the module cache before each test."""
    discovery._META_CACHE.invalidate()
    yield
    discovery._META_CACHE.invalidate()


# ---------------------------------------------------------------------------
# Fake httpx.AsyncClient helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: Dict[str, Any]):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("GET", "https://example/"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class _FakeClient:
    """Minimal async-context-manager stand-in for ``httpx.AsyncClient``.

    ``get_handler`` and ``post_handler`` return ``_FakeResponse`` based on
    the URL; tests pass in whichever they need.
    """

    def __init__(self, *, get_handler=None, post_handler=None):
        self._get = get_handler or (lambda url, **kw: _FakeResponse(404, {}))
        self._post = post_handler or (lambda url, **kw: _FakeResponse(404, {}))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        return self._get(url, **kwargs)

    async def post(self, url, **kwargs):
        return self._post(url, **kwargs)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _anthropic_page(
    models: List[Dict[str, Any]], has_more: bool = False, last_id: str = ""
) -> Dict[str, Any]:
    return {"data": models, "has_more": has_more, "last_id": last_id}


def test_anthropic_requires_key():
    with pytest.raises(RuntimeError):
        asyncio.run(discovery.fetch_anthropic_models(""))


def test_anthropic_single_page_parses_fields():
    pages = [
        _anthropic_page(
            [
                {
                    "id": "claude-haiku-3-5-20241022",
                    "display_name": "Claude Haiku 3.5",
                    "max_input_tokens": 200000,
                    "capabilities": {
                        "image_input": {"supported": True},
                        "thinking": {"supported": False},
                    },
                },
                {
                    "id": "claude-opus-4-7",
                    "display_name": "Claude Opus 4.7",
                    "max_input_tokens": 1000000,
                    "capabilities": {
                        "image_input": {"supported": True},
                        "thinking": {"supported": True},
                    },
                },
            ]
        )
    ]
    calls = {"n": 0}

    def _get(url, **kw):
        calls["n"] += 1
        return _FakeResponse(200, pages[0])

    with patch.object(
        discovery.httpx, "AsyncClient", lambda **kw: _FakeClient(get_handler=_get)
    ):
        out = asyncio.run(discovery.fetch_anthropic_models("sk-test"))

    assert [m.id for m in out] == ["claude-haiku-3-5-20241022", "claude-opus-4-7"]
    haiku = out[0]
    assert haiku.display_name == "Claude Haiku 3.5"
    assert haiku.context_window == 200000
    assert haiku.capabilities["supports_vision"] is True
    assert haiku.capabilities["supports_thinking"] is False
    # Tool support is True for every Claude id (API doesn't expose a flag).
    assert haiku.capabilities["supports_tools"] is True
    assert calls["n"] == 1


def test_anthropic_paginates():
    page1 = _anthropic_page(
        [{"id": "a", "display_name": "A", "max_input_tokens": 1}], True, "a"
    )
    page2 = _anthropic_page(
        [{"id": "b", "display_name": "B", "max_input_tokens": 2}], False, ""
    )

    responses = iter([page1, page2])

    def _get(url, **kw):
        return _FakeResponse(200, next(responses))

    with patch.object(
        discovery.httpx, "AsyncClient", lambda **kw: _FakeClient(get_handler=_get)
    ):
        out = asyncio.run(discovery.fetch_anthropic_models("sk-test"))

    assert [m.id for m in out] == ["a", "b"]


def test_anthropic_retries_on_connect_error(monkeypatch):
    monkeypatch.setattr(discovery, "_RETRY_BACKOFF_S", 0.0)
    calls = {"n": 0}

    def _get(url, **kw):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("boom")
        return _FakeResponse(
            200,
            _anthropic_page([{"id": "x", "display_name": "X", "max_input_tokens": 1}]),
        )

    with patch.object(
        discovery.httpx, "AsyncClient", lambda **kw: _FakeClient(get_handler=_get)
    ):
        out = asyncio.run(discovery.fetch_anthropic_models("sk-test"))
    assert [m.id for m in out] == ["x"]
    assert calls["n"] == 2


def test_anthropic_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(discovery, "_RETRY_BACKOFF_S", 0.0)

    def _get(url, **kw):
        raise httpx.ConnectError("down")

    with patch.object(
        discovery.httpx, "AsyncClient", lambda **kw: _FakeClient(get_handler=_get)
    ):
        with pytest.raises(httpx.ConnectError):
            asyncio.run(discovery.fetch_anthropic_models("sk-test"))


def test_anthropic_cache_hit_skips_httpx():
    pages = [_anthropic_page([{"id": "a", "display_name": "A", "max_input_tokens": 1}])]
    calls = {"n": 0}

    def _get(url, **kw):
        calls["n"] += 1
        return _FakeResponse(200, pages[0])

    with patch.object(
        discovery.httpx, "AsyncClient", lambda **kw: _FakeClient(get_handler=_get)
    ):
        asyncio.run(discovery.fetch_anthropic_models("sk-test"))
        asyncio.run(discovery.fetch_anthropic_models("sk-test"))
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


def test_openai_requires_key():
    with pytest.raises(RuntimeError):
        asyncio.run(discovery.fetch_openai_models(""))


def test_openai_parses_flat_id_list():
    payload = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": None}]}

    def _get(url, **kw):
        assert url.endswith("/models")
        return _FakeResponse(200, payload)

    with patch.object(
        discovery.httpx, "AsyncClient", lambda **kw: _FakeClient(get_handler=_get)
    ):
        out = asyncio.run(discovery.fetch_openai_models("sk-test"))

    assert [m.id for m in out] == ["gpt-4o", "gpt-4o-mini"]
    # OpenAI endpoint doesn't carry meta, so context defaults to 0.
    assert out[0].context_window == 0
    assert out[0].display_name == "gpt-4o"


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def test_ollama_extracts_context_from_show():
    tags = {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5:7b"}]}
    show_llama = {"model_info": {"llama.context_length": 131072}}
    show_qwen = {"model_info": {"qwen2.context_length": 32768}}

    def _get(url, **kw):
        assert url.endswith("/api/tags")
        return _FakeResponse(200, tags)

    def _post(url, **kw):
        name = kw["json"]["name"]
        if name == "llama3.1:8b":
            return _FakeResponse(200, show_llama)
        return _FakeResponse(200, show_qwen)

    with patch.object(
        discovery.httpx,
        "AsyncClient",
        lambda **kw: _FakeClient(get_handler=_get, post_handler=_post),
    ):
        out = asyncio.run(discovery.fetch_ollama_models("http://localhost:11434"))

    by_id = {m.id: m for m in out}
    assert by_id["llama3.1:8b"].context_window == 131072
    assert by_id["qwen2.5:7b"].context_window == 32768


def test_ollama_context_defaults_to_zero_when_show_fails():
    tags = {"models": [{"name": "unknown-model"}]}

    def _get(url, **kw):
        return _FakeResponse(200, tags)

    def _post(url, **kw):
        raise httpx.ReadTimeout("slow")

    with patch.object(
        discovery.httpx,
        "AsyncClient",
        lambda **kw: _FakeClient(get_handler=_get, post_handler=_post),
    ):
        out = asyncio.run(discovery.fetch_ollama_models())

    assert out[0].id == "unknown-model"
    assert out[0].context_window == 0


# ---------------------------------------------------------------------------
# fetch_provider_models — cache reader backed by sync_all_provider_models
# ---------------------------------------------------------------------------


class _FakeRow:
    def __init__(self, provider_type, provider_id="p1", base_url=None, config=None):
        self.provider_type = provider_type
        self.provider_id = provider_id
        self.base_url = base_url
        self.config = config or {}
        self.api_key_ref = None


def _reset_registry_state():
    from services import model_registry

    model_registry._MODEL_LIST_CACHE.invalidate()
    model_registry._EXTRA_IDS.clear()
    model_registry.clear_live_meta()


def test_fetch_provider_models_reads_cache():
    """Populated cache is returned without any lazy sync."""
    from services import model_registry

    _reset_registry_state()
    model_registry._MODEL_LIST_CACHE.set(
        "p1",
        ["model-a", "model-b", "model-c"],
    )
    result = asyncio.run(model_registry.fetch_provider_models(_FakeRow("anthropic")))
    assert result == ["model-a", "model-b", "model-c"]
    _reset_registry_state()


def test_fetch_provider_models_lazy_sync_on_miss(monkeypatch):
    """Cache miss triggers sync_all_provider_models; result is the cache
    entry that sync wrote."""
    from services import model_registry

    _reset_registry_state()
    call_count = {"n": 0}

    async def fake_sync():
        call_count["n"] += 1
        # Simulate the sync populating the cache.
        model_registry._MODEL_LIST_CACHE.set(
            "p1",
            ["from-sync-a", "from-sync-b"],
        )
        return {"bifrost": {}, "models_by_provider": {}}

    # The lazy-sync import is inside the function, so patch the attribute
    # where the caller looks it up.
    import services.bifrost_admin as bifrost_admin

    monkeypatch.setattr(bifrost_admin, "sync_all_provider_models", fake_sync)

    result = asyncio.run(model_registry.fetch_provider_models(_FakeRow("anthropic")))
    assert result == ["from-sync-a", "from-sync-b"]
    assert call_count["n"] == 1
    _reset_registry_state()


def test_fetch_provider_models_hard_fallback_when_sync_fails(monkeypatch):
    """If sync raises AND cache remains empty, return bootstrap + extras."""
    from services import model_registry

    _reset_registry_state()

    async def fake_sync_fails():
        raise RuntimeError("no db")

    import services.bifrost_admin as bifrost_admin

    monkeypatch.setattr(bifrost_admin, "sync_all_provider_models", fake_sync_fails)
    monkeypatch.setenv("ANTHROPIC_EXTRA_MODELS", "legacy-only-1")

    result = asyncio.run(model_registry.fetch_provider_models(_FakeRow("anthropic")))

    # Extras still appear in hard-fallback mode.
    assert "legacy-only-1" in result
    # Bootstrap list is preserved.
    assert "claude-opus-4-7" in result
    _reset_registry_state()
