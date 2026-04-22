"""Model registry — aggregates model metadata and resolves per-component assignments.

Sits on top of the multi-provider layer introduced in #88:
  - Reads `ai_model_configs` (component → provider+model) for assignments.
  - Reads `llm_provider_configs` for provider info.
  - Live-queries providers for their current model lists (Anthropic static,
    Ollama /api/tags, OpenAI /v1/models), with a short TTL cache so the
    Settings UI feels responsive without hammering external APIs.
  - Owns the static cost/capability catalog used for dollar-per-token math
    and the capability chips shown in the UI.

The registry is intentionally decoupled from the DB hot path: all DB access
goes through short-lived sessions that are closed before returning.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO / "backend") not in sys.path:
    sys.path.insert(0, str(_REPO / "backend"))
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Component enum (mirrors ai_model_configs.component values)
# ---------------------------------------------------------------------------

COMPONENTS: Tuple[str, ...] = (
    "chat_default",
    "triage",
    "investigation",
    "orchestrator_plan",
    "orchestrator_review",
    "summarization",
    "reporting",
)


def is_valid_component(name: str) -> bool:
    return name in COMPONENTS


# ---------------------------------------------------------------------------
# Static pricing + capability catalog (per-million-token USD rates)
# ---------------------------------------------------------------------------

# Values sourced from provider public pricing pages as of 2025-Q4. Ollama
# models are self-hosted so rates are $0. When a model isn't in the catalog
# we return (0, 0) and log a warning so cost data degrades gracefully.

_CATALOG: Dict[Tuple[str, str], Dict[str, Any]] = {
    # (provider_type, model_id) → metadata
    ("anthropic", "claude-sonnet-4-5-20250929"): {
        "display_name": "Claude Sonnet 4.5",
        "context_window": 200_000,
        "input_per_m": 3.0,
        "output_per_m": 15.0,
        "supports_tools": True,
        "supports_thinking": True,
        "supports_vision": True,
    },
    ("anthropic", "claude-sonnet-4-20250514"): {
        "display_name": "Claude Sonnet 4",
        "context_window": 200_000,
        "input_per_m": 3.0,
        "output_per_m": 15.0,
        "supports_tools": True,
        "supports_thinking": True,
        "supports_vision": True,
    },
    ("anthropic", "claude-opus-4-20250514"): {
        "display_name": "Claude Opus 4",
        "context_window": 200_000,
        "input_per_m": 15.0,
        "output_per_m": 75.0,
        "supports_tools": True,
        "supports_thinking": True,
        "supports_vision": True,
    },
    ("anthropic", "claude-haiku-4-5-20251001"): {
        "display_name": "Claude Haiku 4.5",
        "context_window": 200_000,
        "input_per_m": 0.80,
        "output_per_m": 4.0,
        "supports_tools": True,
        "supports_thinking": False,
        "supports_vision": True,
    },
    ("openai", "gpt-4o"): {
        "display_name": "GPT-4o",
        "context_window": 128_000,
        "input_per_m": 2.50,
        "output_per_m": 10.0,
        "supports_tools": True,
        "supports_thinking": False,
        "supports_vision": True,
    },
    ("openai", "gpt-4o-mini"): {
        "display_name": "GPT-4o mini",
        "context_window": 128_000,
        "input_per_m": 0.15,
        "output_per_m": 0.60,
        "supports_tools": True,
        "supports_thinking": False,
        "supports_vision": True,
    },
    ("openai", "gpt-4-turbo"): {
        "display_name": "GPT-4 Turbo",
        "context_window": 128_000,
        "input_per_m": 10.0,
        "output_per_m": 30.0,
        "supports_tools": True,
        "supports_thinking": False,
        "supports_vision": True,
    },
}


def _catalog_entry(provider_type: str, model_id: str) -> Dict[str, Any]:
    """Return catalog entry or a safe default."""
    entry = _CATALOG.get((provider_type, model_id))
    if entry is not None:
        return entry
    if provider_type == "ollama":
        return {
            "display_name": model_id,
            "context_window": 0,  # unknown — depends on ollama modelfile
            "input_per_m": 0.0,
            "output_per_m": 0.0,
            "supports_tools": False,
            "supports_thinking": False,
            "supports_vision": False,
        }
    logger.warning(
        "No catalog entry for %s/%s — defaulting cost to $0 and capabilities to false",
        provider_type,
        model_id,
    )
    return {
        "display_name": model_id,
        "context_window": 0,
        "input_per_m": 0.0,
        "output_per_m": 0.0,
        "supports_tools": False,
        "supports_thinking": False,
        "supports_vision": False,
    }


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    provider_id: str
    provider_type: str
    display_name: str
    context_window: int
    input_cost_per_1k: float  # USD — per 1,000 tokens
    output_cost_per_1k: float
    supports_tools: bool
    supports_thinking: bool
    supports_vision: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "provider_type": self.provider_type,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "input_cost_per_1k": self.input_cost_per_1k,
            "output_cost_per_1k": self.output_cost_per_1k,
            "supports_tools": self.supports_tools,
            "supports_thinking": self.supports_thinking,
            "supports_vision": self.supports_vision,
        }


@dataclass(frozen=True)
class ComponentAssignment:
    component: str
    provider_id: str
    model_id: str
    settings: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider model lists — live-query with a per-provider TTL cache
# ---------------------------------------------------------------------------

_MODEL_LIST_CACHE_TTL_S = 60.0


class _ProviderModelCache:
    def __init__(self):
        self._entries: Dict[str, Tuple[float, List[str]]] = {}

    def get(self, provider_id: str) -> Optional[List[str]]:
        hit = self._entries.get(provider_id)
        if not hit:
            return None
        ts, models = hit
        if time.time() - ts > _MODEL_LIST_CACHE_TTL_S:
            return None
        return models

    def set(self, provider_id: str, models: List[str]) -> None:
        self._entries[provider_id] = (time.time(), models)

    def invalidate(self, provider_id: Optional[str] = None) -> None:
        if provider_id is None:
            self._entries.clear()
        else:
            self._entries.pop(provider_id, None)


_MODEL_LIST_CACHE = _ProviderModelCache()


# Kept in sync with docker/bifrost/config.json and backend/api/llm_providers.py.
ANTHROPIC_STATIC_MODELS: Tuple[str, ...] = (
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
    "claude-haiku-4-5-20251001",
)


async def fetch_provider_models(row) -> List[str]:
    """Live-query a provider for its available model IDs.

    ``row`` is an LLMProviderConfig ORM row. Raises on unrecoverable errors;
    callers should catch and degrade gracefully (e.g. fall back to
    [row.default_model]).
    """
    import httpx  # lazy

    cached = _MODEL_LIST_CACHE.get(row.provider_id)
    if cached is not None:
        return cached

    provider_type = row.provider_type
    models: List[str]

    if provider_type == "anthropic":
        models = list(ANTHROPIC_STATIC_MODELS)

    elif provider_type == "ollama":
        base_url = (row.base_url or "http://localhost:11434").rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        models = [m.get("name") for m in data.get("models", []) if m.get("name")]

    elif provider_type == "openai":
        base_url = (row.base_url or "https://api.openai.com/v1").rstrip("/")
        api_key = await _resolve_provider_key(row)
        if not api_key:
            raise RuntimeError(f"openai provider {row.provider_id}: no api key")
        headers = {"Authorization": f"Bearer {api_key}"}
        if row.config and row.config.get("organization"):
            headers["OpenAI-Organization"] = row.config["organization"]
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]

    else:
        raise RuntimeError(f"unsupported provider_type: {provider_type}")

    _MODEL_LIST_CACHE.set(row.provider_id, models)
    return models


async def _resolve_provider_key(row) -> Optional[str]:
    """Resolve a provider's API key via secrets_manager with env fallbacks."""
    try:
        from secrets_manager import get_secret  # type: ignore
    except Exception:
        get_secret = None  # type: ignore

    if row.api_key_ref and get_secret is not None:
        try:
            key = get_secret(row.api_key_ref)
            if key:
                return key
        except Exception as exc:  # noqa: BLE001
            logger.debug("secret lookup for %s failed: %s", row.api_key_ref, exc)

    if row.provider_type == "anthropic":
        return os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    if row.provider_type == "openai":
        return os.getenv("OPENAI_API_KEY")
    return None


# ---------------------------------------------------------------------------
# ModelRegistry
# ---------------------------------------------------------------------------


class ModelRegistry:
    """Per-component model resolution + aggregated model listings.

    Safe to instantiate directly; the module-level ``get_registry()``
    returns a shared default instance.
    """

    # ---- cost lookup (pure) ----------------------------------------------

    @staticmethod
    def get_cost_rates(model_id: str, provider_type: str) -> Tuple[float, float]:
        """Return (input_cost_per_token, output_cost_per_token) in USD.

        Returns (0.0, 0.0) for unknown models and logs a warning (handled
        inside ``_catalog_entry``).
        """
        entry = _catalog_entry(provider_type, model_id)
        return (entry["input_per_m"] / 1_000_000, entry["output_per_m"] / 1_000_000)

    @staticmethod
    def get_model_info(
        provider_id: str, provider_type: str, model_id: str
    ) -> ModelInfo:
        entry = _catalog_entry(provider_type, model_id)
        return ModelInfo(
            model_id=model_id,
            provider_id=provider_id,
            provider_type=provider_type,
            display_name=entry["display_name"],
            context_window=entry["context_window"],
            input_cost_per_1k=entry["input_per_m"] / 1000,
            output_cost_per_1k=entry["output_per_m"] / 1000,
            supports_tools=entry["supports_tools"],
            supports_thinking=entry["supports_thinking"],
            supports_vision=entry["supports_vision"],
        )

    # ---- assignments -----------------------------------------------------

    def get_component_assignment(self, component: str) -> Optional[ComponentAssignment]:
        """Load a single `ai_model_configs` row. Returns None on cache miss."""
        try:
            from database.connection import get_db_session
            from database.models import AIModelConfig
        except Exception as exc:  # noqa: BLE001
            logger.debug("AIModelConfig lookup skipped: %s", exc)
            return None

        session = get_db_session()
        try:
            row = session.get(AIModelConfig, component)
            if row is None:
                return None
            return ComponentAssignment(
                component=row.component,
                provider_id=row.provider_id,
                model_id=row.model_id,
                settings=dict(row.settings or {}),
            )
        finally:
            session.close()

    def get_all_assignments(self) -> Dict[str, ComponentAssignment]:
        """Return every configured assignment keyed by component."""
        try:
            from database.connection import get_db_session
            from database.models import AIModelConfig
        except Exception as exc:  # noqa: BLE001
            logger.debug("AIModelConfig listing skipped: %s", exc)
            return {}

        session = get_db_session()
        try:
            rows = session.query(AIModelConfig).all()
            return {
                r.component: ComponentAssignment(
                    component=r.component,
                    provider_id=r.provider_id,
                    model_id=r.model_id,
                    settings=dict(r.settings or {}),
                )
                for r in rows
            }
        finally:
            session.close()

    def resolve_model_for_component(
        self, component: str, *, agent_override: Optional[str] = None
    ) -> Optional[Tuple[str, str]]:
        """Return (provider_id, model_id) using the fallback chain.

        Chain:
          agent_override (if set) resolves through the default Anthropic provider,
            since we don't know which provider owns a raw model id
          → ai_model_configs[component]
          → ai_model_configs['chat_default']
          → default Anthropic provider's default_model

        Returns None only if no DB is reachable and there's no Anthropic default.
        """
        if agent_override:
            # agent-level overrides carry only a model id. Attach it to the
            # default Anthropic provider — this is the historical assumption
            # and matches how per-agent models worked before #89.
            default_anthropic = self._default_anthropic_provider()
            if default_anthropic is not None:
                return (default_anthropic["provider_id"], agent_override)

        assignments = self.get_all_assignments()
        if component in assignments:
            a = assignments[component]
            return (a.provider_id, a.model_id)
        if "chat_default" in assignments:
            a = assignments["chat_default"]
            return (a.provider_id, a.model_id)

        default_anthropic = self._default_anthropic_provider()
        if default_anthropic is not None:
            return (
                default_anthropic["provider_id"],
                default_anthropic["default_model"],
            )
        return None

    # ---- provider helpers ------------------------------------------------

    def _default_anthropic_provider(self) -> Optional[Dict[str, str]]:
        try:
            from database.connection import get_db_session
            from database.models import LLMProviderConfig
        except Exception:
            return None
        session = get_db_session()
        try:
            row = (
                session.query(LLMProviderConfig)
                .filter(
                    LLMProviderConfig.provider_type == "anthropic",
                    LLMProviderConfig.is_default.is_(True),
                )
                .first()
            )
            if row is None:
                return None
            return {"provider_id": row.provider_id, "default_model": row.default_model}
        finally:
            session.close()

    async def list_available_models(self) -> List[ModelInfo]:
        """Aggregate live model lists across all active providers.

        Each provider is queried independently; a provider failure is
        logged but never blocks the overall list.
        """
        try:
            from database.connection import get_db_session
            from database.models import LLMProviderConfig
        except Exception as exc:  # noqa: BLE001
            logger.debug("list_available_models: DB unreachable: %s", exc)
            return []

        session = get_db_session()
        try:
            providers = (
                session.query(LLMProviderConfig)
                .filter(LLMProviderConfig.is_active.is_(True))
                .all()
            )
        finally:
            session.close()

        out: List[ModelInfo] = []
        seen: set = set()

        for p in providers:
            try:
                model_ids = await fetch_provider_models(p)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to fetch models for provider %s (%s): %s — "
                    "falling back to default_model",
                    p.provider_id,
                    p.provider_type,
                    exc,
                )
                model_ids = [p.default_model] if p.default_model else []

            for mid in model_ids:
                key = (p.provider_id, mid)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    self.get_model_info(
                        provider_id=p.provider_id,
                        provider_type=p.provider_type,
                        model_id=mid,
                    )
                )

        return out


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------

_registry_singleton: Optional[ModelRegistry] = None


def get_registry() -> ModelRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = ModelRegistry()
    return _registry_singleton


def invalidate_model_cache(provider_id: Optional[str] = None) -> None:
    """Callers that change provider config (add/update/delete) can drop
    the per-provider model-list cache so the UI sees fresh data."""
    _MODEL_LIST_CACHE.invalidate(provider_id)
