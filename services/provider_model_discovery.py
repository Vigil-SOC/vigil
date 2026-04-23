"""Per-provider live model discovery.

One module, three providers, one normalized return shape. Each provider's
public catalog endpoint is queried directly (not through Bifrost — this is
capability discovery, not LLM traffic, so the "single LLM routing path"
policy doesn't apply: the same carve-out already applies to
``backend/api/llm_providers.py::test_provider`` which validates user keys
against upstream).

Returned shape — ``ModelMeta``:

    {
        "id": "<model-id>",
        "display_name": "<human-readable>",
        "context_window": <int, 0 when unknown>,
        "capabilities": {
            "supports_tools": bool,
            "supports_thinking": bool,
            "supports_vision": bool,
        },
    }

Each function retries on transient connection failures, TTL-caches on the
tuple ``(provider_type, base_url, key_hash)``, and returns ``[]`` when the
upstream is reachable but returns nothing so the caller can distinguish
"no models" from "discovery failed" (latter raises).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 60.0
_RETRIES = 3
_RETRY_BACKOFF_S = 2.0
_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class ModelMeta:
    id: str
    display_name: str
    context_window: int = 0
    capabilities: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "capabilities": dict(self.capabilities),
        }


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class _MetaCache:
    def __init__(self) -> None:
        self._entries: Dict[str, Tuple[float, List[ModelMeta]]] = {}

    def get(self, key: str) -> Optional[List[ModelMeta]]:
        hit = self._entries.get(key)
        if not hit:
            return None
        ts, models = hit
        if time.time() - ts > _CACHE_TTL_S:
            return None
        return models

    def set(self, key: str, models: List[ModelMeta]) -> None:
        self._entries[key] = (time.time(), models)

    def invalidate(self, key: Optional[str] = None) -> None:
        if key is None:
            self._entries.clear()
        else:
            self._entries.pop(key, None)


_META_CACHE = _MetaCache()


def _cache_key(provider_type: str, base_url: str, secret: str) -> str:
    material = f"{provider_type}|{base_url}|{secret}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def invalidate_cache(key: Optional[str] = None) -> None:
    """Drop cached meta. Called from refresh endpoints and when provider
    config changes."""
    _META_CACHE.invalidate(key)


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


async def _with_retry(label: str, coro_factory) -> Any:
    """Run ``coro_factory()`` with 3 tries and 2s backoff on ConnectionError /
    httpx transient errors. Non-connection HTTP errors pass through immediately."""
    last: Optional[Exception] = None
    for attempt in range(1, _RETRIES + 1):
        try:
            return await coro_factory()
        except (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ) as exc:
            last = exc
            logger.debug("%s: attempt %d/%d failed (%s)", label, attempt, _RETRIES, exc)
            if attempt < _RETRIES:
                await asyncio.sleep(_RETRY_BACKOFF_S)
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def _anthropic_caps(api_caps: Dict[str, Any]) -> Dict[str, bool]:
    """Map the Anthropic /v1/models capability block onto our flat booleans."""
    thinking = api_caps.get("thinking") or {}
    image = api_caps.get("image_input") or {}
    # All current Claude models support tool-use. The API doesn't expose a
    # per-model flag for it (there's no ``tools`` sub-object in the response),
    # so we default to True for every Claude id.
    return {
        "supports_tools": True,
        "supports_thinking": bool(thinking.get("supported", False)),
        "supports_vision": bool(image.get("supported", False)),
    }


async def fetch_anthropic_models(api_key: str) -> List[ModelMeta]:
    """Fetch the live Anthropic model catalog.

    Raises on unrecoverable error so the caller can fall back to the
    hard-coded bootstrap list. A non-200 from Anthropic (e.g. invalid
    key) raises ``httpx.HTTPStatusError``.
    """
    if not api_key:
        raise RuntimeError("fetch_anthropic_models: api_key required")

    cache_key = _cache_key("anthropic", _ANTHROPIC_API_URL, api_key)
    cached = _META_CACHE.get(cache_key)
    if cached is not None:
        return cached

    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
    }

    async def _call() -> List[ModelMeta]:
        out: List[ModelMeta] = []
        after_id: Optional[str] = None
        async with httpx.AsyncClient(timeout=15.0) as client:
            while True:
                params: Dict[str, Any] = {"limit": 1000}
                if after_id:
                    params["after_id"] = after_id
                resp = await client.get(
                    _ANTHROPIC_API_URL, headers=headers, params=params
                )
                resp.raise_for_status()
                payload = resp.json()
                for m in payload.get("data", []):
                    mid = m.get("id")
                    if not mid:
                        continue
                    out.append(
                        ModelMeta(
                            id=mid,
                            display_name=m.get("display_name") or mid,
                            context_window=int(m.get("max_input_tokens") or 0),
                            capabilities=_anthropic_caps(m.get("capabilities") or {}),
                        )
                    )
                if not payload.get("has_more"):
                    break
                after_id = payload.get("last_id")
                if not after_id:
                    break
        return out

    models = await _with_retry("anthropic model fetch", _call)
    _META_CACHE.set(cache_key, models)
    return models


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------


async def fetch_openai_models(
    api_key: str,
    base_url: Optional[str] = None,
    organization: Optional[str] = None,
) -> List[ModelMeta]:
    """Fetch the live OpenAI (or OpenAI-compatible) model catalog.

    OpenAI's /v1/models returns only ``id``/``created``/``owned_by``; no
    display name, context, or capability data. The model_registry tier
    heuristic fills in pricing — context/capabilities stay at their
    (0/False) defaults unless an override is registered in the static
    catalog.
    """
    if not api_key:
        raise RuntimeError("fetch_openai_models: api_key required")

    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    cache_key = _cache_key("openai", base, api_key + "|" + (organization or ""))
    cached = _META_CACHE.get(cache_key)
    if cached is not None:
        return cached

    headers = {"Authorization": f"Bearer {api_key}"}
    if organization:
        headers["OpenAI-Organization"] = organization

    async def _call() -> List[ModelMeta]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{base}/models", headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        out: List[ModelMeta] = []
        for m in payload.get("data", []):
            mid = m.get("id")
            if not mid:
                continue
            out.append(ModelMeta(id=mid, display_name=mid))
        return out

    models = await _with_retry("openai model fetch", _call)
    _META_CACHE.set(cache_key, models)
    return models


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


def _ollama_context_from_show(show_payload: Dict[str, Any]) -> int:
    """Extract a context window from the ``/api/show`` response.

    Ollama nests context under ``model_info`` with architecture-specific keys
    like ``llama.context_length`` or ``qwen2.context_length``. We scan for the
    first key ending in ``.context_length``.
    """
    info = show_payload.get("model_info") or {}
    for key, value in info.items():
        if key.endswith(".context_length"):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
    return 0


async def fetch_ollama_models(base_url: Optional[str] = None) -> List[ModelMeta]:
    """Fetch the Ollama library with a best-effort ``/api/show`` probe for
    context windows. A failed per-model probe doesn't abort the batch."""
    base = (base_url or "http://localhost:11434").rstrip("/")
    cache_key = _cache_key("ollama", base, "")
    cached = _META_CACHE.get(cache_key)
    if cached is not None:
        return cached

    async def _list() -> List[str]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}/api/tags")
            resp.raise_for_status()
            payload = resp.json()
        return [m.get("name") for m in payload.get("models", []) if m.get("name")]

    names = await _with_retry("ollama tags fetch", _list)

    async def _show(client: httpx.AsyncClient, name: str) -> ModelMeta:
        try:
            resp = await client.post(f"{base}/api/show", json={"name": name})
            resp.raise_for_status()
            ctx = _ollama_context_from_show(resp.json())
        except Exception as exc:  # noqa: BLE001
            logger.debug("ollama /api/show %s failed: %s", name, exc)
            ctx = 0
        return ModelMeta(id=name, display_name=name, context_window=ctx)

    async with httpx.AsyncClient(timeout=10.0) as client:
        models = await asyncio.gather(*(_show(client, n) for n in names))

    _META_CACHE.set(cache_key, list(models))
    return list(models)
