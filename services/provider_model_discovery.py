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
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services.url_safety import UrlSafetyError, validate_provider_url

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 60.0
_RETRIES = 3
_RETRY_BACKOFF_S = 2.0
_ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
_ANTHROPIC_VERSION = "2023-06-01"

# Response-size cap for upstream discovery responses. Tight by design —
# we only need a list of model IDs. Anything larger is suspicious.
_MAX_RESPONSE_BYTES = 1 * 1024 * 1024


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


async def fetch_anthropic_models(
    api_key: str,
    base_url: Optional[str] = None,
) -> List[ModelMeta]:
    """Fetch the live Anthropic (or Anthropic-compatible) model catalog.

    Raises on unrecoverable error so the caller can fall back to the
    hard-coded bootstrap list. A non-200 from upstream (e.g. invalid
    key) raises ``httpx.HTTPStatusError``.

    ``base_url`` defaults to ``https://api.anthropic.com/v1``. Override
    for on-prem / private Anthropic-compatible deployments. The full
    models URL is derived as ``{base_url}/models``.

    The URL is run through :func:`services.url_safety.validate_provider_url`
    before any request — it must use http/https, must not point at a
    loopback/private/link-local address (unless the host is in the
    public allowlist), and any query string is stripped. Bearer/x-api-key
    headers are dropped for non-allowlisted hosts so a user-supplied
    URL can never exfiltrate the configured key.
    """
    if not api_key:
        raise RuntimeError("fetch_anthropic_models: api_key required")

    try:
        safe = validate_provider_url(
            base_url or _ANTHROPIC_DEFAULT_BASE_URL, allow_custom=True
        )
    except UrlSafetyError as exc:
        raise RuntimeError(str(exc)) from exc

    base = safe.sanitized.rstrip("/")
    models_url = f"{base}/models"
    cache_key = _cache_key("anthropic", models_url, api_key)
    cached = _META_CACHE.get(cache_key)
    if cached is not None:
        return cached

    headers: Dict[str, str] = {"anthropic-version": _ANTHROPIC_VERSION}
    # Only attach the API key when targeting an allowlisted public host.
    # Otherwise a misconfigured custom base_url would leak the key to a
    # third party.
    if safe.is_allowlisted_host:
        headers["x-api-key"] = api_key

    async def _call() -> List[ModelMeta]:
        out: List[ModelMeta] = []
        after_id: Optional[str] = None
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            while True:
                params: Dict[str, Any] = {"limit": 1000}
                if after_id:
                    params["after_id"] = after_id
                resp = await client.get(models_url, headers=headers, params=params)
                resp.raise_for_status()
                if len(getattr(resp, "content", b"") or b"") > _MAX_RESPONSE_BYTES:
                    raise RuntimeError("upstream response exceeded size cap")
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

    The URL is validated by :func:`services.url_safety.validate_provider_url`
    before any request, and the bearer token is omitted when targeting
    non-allowlisted hosts so user-supplied custom URLs can't exfiltrate
    the configured key (see 2026-05 SSRF disclosure).
    """
    if not api_key:
        raise RuntimeError("fetch_openai_models: api_key required")

    try:
        safe = validate_provider_url(
            base_url or "https://api.openai.com/v1", allow_custom=True
        )
    except UrlSafetyError as exc:
        raise RuntimeError(str(exc)) from exc

    base = safe.sanitized.rstrip("/")
    cache_key = _cache_key("openai", base, api_key + "|" + (organization or ""))
    cached = _META_CACHE.get(cache_key)
    if cached is not None:
        return cached

    headers: Dict[str, str] = {}
    if safe.is_allowlisted_host:
        headers["Authorization"] = f"Bearer {api_key}"
        if organization:
            headers["OpenAI-Organization"] = organization

    async def _call() -> List[ModelMeta]:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            resp = await client.get(f"{base}/models", headers=headers)
            resp.raise_for_status()
            # ``resp.content`` is bytes on real httpx responses. Test
            # fakes may omit it — gate the cap on attribute presence so
            # unit tests using minimal stubs don't have to mock it.
            if len(getattr(resp, "content", b"") or b"") > _MAX_RESPONSE_BYTES:
                raise RuntimeError("upstream response exceeded size cap")
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


# Ollama model families known to support OpenAI-style tool calling.
# Derived from Ollama docs and empirical testing. The model name (or its
# architecture family from /api/show) is matched case-insensitively.
_OLLAMA_TOOL_CAPABLE_FAMILIES = frozenset(
    (
        "llama3.1",
        "llama3.2",
        "llama3.3",
        "llama4",
        "qwen2.5",
        "qwen3",
        "qwq",
        "mistral",
        "mixtral",
        "mistral-nemo",
        "mistral-small",
        "mistral-large",
        "command-r",
        "command-r-plus",
        "deepseek-r1",
        "deepseek-v2",
        "deepseek-v3",
        "deepseek-coder-v2",
        "nemotron",
        "granite3",
        "phi4",
        "glm4",
        "glm-4",
        "hermes3",
        "athene",
        "firefunction",
    )
)

_OLLAMA_VISION_CAPABLE_FAMILIES = frozenset(
    (
        "llava",
        "llava-llama3",
        "llava-phi3",
        "llama3.2-vision",
        "moondream",
        "bakllava",
        "minicpm-v",
    )
)


def _ollama_env_tool_allowlist() -> frozenset:
    """Operator-supplied tool-capable model names/prefixes.

    ``OLLAMA_EXTRA_TOOL_MODELS`` (comma-separated) lets a deployment mark
    custom/local models as tool-capable when neither /api/tags nor the
    built-in family list knows them.
    """
    raw = os.getenv("OLLAMA_EXTRA_TOOL_MODELS", "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def _name_matches_family(name_lower: str, families) -> bool:
    return any(name_lower.startswith(f) or name_lower == f for f in families)


def _ollama_capabilities_from_show(
    model_name: str,
    show_payload: Dict[str, Any],
    live_caps: Optional[List[str]] = None,
) -> Dict[str, bool]:
    """Infer model capabilities from Ollama metadata.

    Precedence for tool support: the live ``/api/tags`` capabilities are
    authoritative; then an operator allowlist (``OLLAMA_EXTRA_TOOL_MODELS``);
    then name/architecture-family heuristics (logged, since they can lag new
    model releases).
    """
    name_lower = model_name.lower().split(":")[0]
    info = show_payload.get("model_info") or {}
    live_caps = live_caps or []

    families_in_info = set()
    for key in info:
        parts = key.split(".")
        if parts:
            families_in_info.add(parts[0].lower())

    # Tool support, in order of authority.
    if "tools" in live_caps:
        supports_tools = True
    elif _name_matches_family(name_lower, _ollama_env_tool_allowlist()):
        supports_tools = True
        logger.info(
            "ollama: %s marked tool-capable via OLLAMA_EXTRA_TOOL_MODELS",
            model_name,
        )
    else:
        supports_tools = _name_matches_family(name_lower, _OLLAMA_TOOL_CAPABLE_FAMILIES)
        if not supports_tools:
            for arch_family in families_in_info:
                if arch_family in ("general", "tokenizer"):
                    continue
                for known in _OLLAMA_TOOL_CAPABLE_FAMILIES:
                    normalized = known.replace("-", "").replace(".", "")
                    if arch_family.startswith(normalized) or normalized.startswith(
                        arch_family
                    ):
                        supports_tools = True
                        break
                if supports_tools:
                    break
        if supports_tools:
            logger.debug(
                "ollama: %s tool support inferred from name/arch heuristic "
                "(not reported by /api/tags)",
                model_name,
            )

    # Vision support: prefer live capability, else name heuristic.
    supports_vision = "vision" in live_caps or _name_matches_family(
        name_lower, _OLLAMA_VISION_CAPABLE_FAMILIES
    )

    # Ollama models don't have native extended thinking in the Anthropic sense
    return {
        "supports_tools": supports_tools,
        "supports_thinking": False,
        "supports_vision": supports_vision,
    }


async def fetch_ollama_models(
    base_url: Optional[str] = None,
    *,
    allow_loopback: bool = False,
) -> List[ModelMeta]:
    """Fetch the Ollama library with a best-effort ``/api/show`` probe.

    Ollama is the legitimate "self-hosted" provider, so a loopback URL
    is the expected default. The caller can set ``allow_loopback=True``
    if (and only if) it has authenticated an admin who actually wants
    to probe ``localhost`` — otherwise the URL is run through the same
    SSRF gate as the public providers.

    The route handler in ``backend/api/llm_providers.py`` decides
    whether to pass ``allow_loopback=True`` based on the authenticated
    caller's permissions.
    """
    raw_base = base_url or "http://localhost:11434"

    if allow_loopback:
        # Admin opted in. Still pipe through the parser to drop query/
        # userinfo/fragment and reject non-http(s) schemes.
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(raw_base.strip())
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError(f"scheme not allowed: {parsed.scheme}")
        if parsed.username or parsed.password or parsed.fragment:
            raise RuntimeError("ollama base_url must not include userinfo or fragment")
        base = urlunparse(
            (
                parsed.scheme,
                parsed.netloc.split("@")[-1],
                parsed.path or "",
                "",
                "",
                "",
            )
        ).rstrip("/")
    else:
        try:
            safe = validate_provider_url(raw_base, allow_custom=True)
        except UrlSafetyError as exc:
            raise RuntimeError(str(exc)) from exc
        base = safe.sanitized.rstrip("/")

    cache_key = _cache_key("ollama", base, "")
    cached = _META_CACHE.get(cache_key)
    if cached is not None:
        return cached

    async def _list() -> List[Dict[str, Any]]:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            resp = await client.get(f"{base}/api/tags")
            resp.raise_for_status()
            if len(getattr(resp, "content", b"") or b"") > _MAX_RESPONSE_BYTES:
                raise RuntimeError("upstream response exceeded size cap")
            payload = resp.json()
        return [m for m in payload.get("models", []) if m.get("name")]

    tag_entries = await _with_retry("ollama tags fetch", _list)
    names = [m["name"] for m in tag_entries]
    # Build a lookup for capabilities reported by /api/tags
    tags_caps: Dict[str, List[str]] = {}
    for entry in tag_entries:
        tags_caps[entry["name"]] = entry.get("capabilities") or []

    async def _show(client: httpx.AsyncClient, name: str) -> ModelMeta:
        try:
            resp = await client.post(f"{base}/api/show", json={"name": name})
            resp.raise_for_status()
            if len(getattr(resp, "content", b"") or b"") > _MAX_RESPONSE_BYTES:
                raise RuntimeError("upstream response exceeded size cap")
            payload = resp.json()
            ctx = _ollama_context_from_show(payload)
            caps = _ollama_capabilities_from_show(
                name, payload, live_caps=tags_caps.get(name, [])
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("ollama /api/show %s failed: %s", name, exc)
            ctx = 0
            caps = _ollama_capabilities_from_show(
                name, {}, live_caps=tags_caps.get(name, [])
            )
        return ModelMeta(
            id=name,
            display_name=name,
            context_window=ctx,
            capabilities=caps,
        )

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        models = await asyncio.gather(*(_show(client, n) for n in names))

    _META_CACHE.set(cache_key, list(models))
    return list(models)
