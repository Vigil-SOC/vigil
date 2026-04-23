"""Model registry — aggregates model metadata and resolves per-component assignments.

Sits on top of the multi-provider layer introduced in #88:
  - Reads `ai_model_configs` (component → provider+model) for assignments.
  - Reads `llm_provider_configs` for provider info.
  - Live-queries providers for their current model lists via
    ``services.provider_model_discovery`` (Anthropic /v1/models, OpenAI
    /v1/models, Ollama /api/tags + /api/show), with a short TTL cache.
  - Owns a layered cost/capability catalog: live upstream meta first, then
    a static override dict for known-exact values, then a provider-specific
    tier heuristic keyed by model-id prefix, then a safe (0, 0) default.

The registry is intentionally decoupled from the DB hot path: all DB access
goes through short-lived sessions that are closed before returning.
"""

from __future__ import annotations

import logging
import os
import re
import sys
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
# Layered catalog — live meta → static override → tier heuristic → default
# ---------------------------------------------------------------------------
#
# 1. Live meta: populated by ``services.provider_model_discovery`` when the
#    dynamic-discovery path runs. Holds display_name, context_window and
#    capability flags scraped from upstream APIs. Does NOT hold pricing —
#    no provider publishes pricing in their /models endpoint.
# 2. Static overrides (``_CATALOG``): exact-known values we've hand-verified
#    against provider pricing pages. Takes precedence over tier heuristics
#    so cost math stays precise for the models we actually use a lot.
# 3. Tier heuristic (``_TIER_HEURISTIC``): prefix-regex fallback. Ensures a
#    new model (e.g. a future Haiku or Sonnet variant) gets a reasonable
#    cost estimate the moment upstream exposes it, without a code change.
# 4. Default: (0, 0) for cost, 0 for context, all-False capabilities.


# Exact overrides — per-million-token USD rates. Sourced from provider
# public pricing pages as of 2025-Q4. Ollama models are self-hosted → $0.

_CATALOG: Dict[Tuple[str, str], Dict[str, Any]] = {
    # (provider_type, model_id) → metadata
    ("anthropic", "claude-opus-4-7"): {
        "display_name": "Claude Opus 4.7",
        "context_window": 1_000_000,
        "input_per_m": 15.0,
        "output_per_m": 75.0,
        "supports_tools": True,
        "supports_thinking": True,
        "supports_vision": True,
    },
    ("anthropic", "claude-sonnet-4-6"): {
        "display_name": "Claude Sonnet 4.6",
        "context_window": 200_000,
        "input_per_m": 3.0,
        "output_per_m": 15.0,
        "supports_tools": True,
        "supports_thinking": True,
        "supports_vision": True,
    },
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
    # Legacy 3.x — kept so the extras-mechanism (see _DEFAULT_EXTRA_MODELS)
    # renders correct context/pricing instead of "0K ctx · $0". Anthropic
    # pulled these from /v1/models but they're still callable.
    ("anthropic", "claude-3-5-sonnet-20241022"): {
        "display_name": "Claude Sonnet 3.5 v2",
        "context_window": 200_000,
        "input_per_m": 3.0,
        "output_per_m": 15.0,
        "supports_tools": True,
        "supports_thinking": False,
        "supports_vision": True,
    },
    ("anthropic", "claude-3-5-haiku-20241022"): {
        "display_name": "Claude Haiku 3.5",
        "context_window": 200_000,
        "input_per_m": 0.80,
        "output_per_m": 4.0,
        "supports_tools": True,
        "supports_thinking": False,
        "supports_vision": False,
    },
    ("anthropic", "claude-3-haiku-20240307"): {
        "display_name": "Claude Haiku 3",
        "context_window": 200_000,
        "input_per_m": 0.25,
        "output_per_m": 1.25,
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


# ---------------------------------------------------------------------------
# Tier heuristic — last-resort pricing by model-id prefix regex
# ---------------------------------------------------------------------------
# Order matters: more specific patterns first. Each entry gives the
# per-million-token USD rate pair. Context window / capabilities are left
# at defaults when a tier heuristic is all we have; live meta (layer 1)
# is expected to fill those in for any model worth routing to.
#
# NOTE: these are estimates. Exact rates belong in ``_CATALOG`` above.

_TierPattern = Tuple[str, float, float]

_ANTHROPIC_TIERS: Tuple[_TierPattern, ...] = (
    (r"opus", 15.0, 75.0),
    (r"sonnet", 3.0, 15.0),
    (r"haiku", 0.80, 4.0),
)

_OPENAI_TIERS: Tuple[_TierPattern, ...] = (
    (r"gpt-4o-mini", 0.15, 0.60),
    (r"gpt-4o", 2.50, 10.0),
    (r"gpt-4\.1-mini", 0.40, 1.60),
    (r"gpt-4\.1", 2.0, 8.0),
    (r"gpt-4-turbo", 10.0, 30.0),
    (r"gpt-4", 30.0, 60.0),
    (r"o3-mini", 1.10, 4.40),
    (r"o3", 10.0, 40.0),
    (r"o1-mini", 1.10, 4.40),
    (r"o1", 15.0, 60.0),
)

_TIER_HEURISTIC: Dict[str, Tuple[_TierPattern, ...]] = {
    "anthropic": _ANTHROPIC_TIERS,
    "openai": _OPENAI_TIERS,
    "ollama": (),  # always $0 — handled as a separate branch
}


def _match_tier(provider_type: str, model_id: str) -> Optional[Tuple[float, float]]:
    for pattern, in_rate, out_rate in _TIER_HEURISTIC.get(provider_type, ()):
        if re.search(pattern, model_id, re.IGNORECASE):
            return (in_rate, out_rate)
    return None


# ---------------------------------------------------------------------------
# Live-meta cache — populated by the discovery module
# ---------------------------------------------------------------------------

# Maps (provider_type, model_id) → catalog-shape dict with the meta we
# got from upstream. Populated by ``record_live_meta`` after
# ``services.provider_model_discovery.fetch_*`` succeeds. Cleared when
# discovery cache is invalidated.

_LIVE_META: Dict[Tuple[str, str], Dict[str, Any]] = {}


def record_live_meta(provider_type: str, meta_list: List[Any]) -> None:
    """Absorb a list of ``ModelMeta`` objects from the discovery module.

    Called by ``fetch_provider_models`` after a successful upstream call
    so subsequent cost/catalog lookups see the live display_name, context,
    and capability data.
    """
    for m in meta_list:
        caps = getattr(m, "capabilities", {}) or {}
        _LIVE_META[(provider_type, m.id)] = {
            "display_name": getattr(m, "display_name", m.id),
            "context_window": int(getattr(m, "context_window", 0) or 0),
            "supports_tools": bool(caps.get("supports_tools", False)),
            "supports_thinking": bool(caps.get("supports_thinking", False)),
            "supports_vision": bool(caps.get("supports_vision", False)),
        }


def clear_live_meta(provider_type: Optional[str] = None) -> None:
    if provider_type is None:
        _LIVE_META.clear()
        return
    for key in list(_LIVE_META.keys()):
        if key[0] == provider_type:
            _LIVE_META.pop(key, None)


# ---------------------------------------------------------------------------
# Layered lookup
# ---------------------------------------------------------------------------


def _default_entry(provider_type: str, model_id: str) -> Dict[str, Any]:
    return {
        "display_name": model_id,
        "context_window": 0,
        "input_per_m": 0.0,
        "output_per_m": 0.0,
        "supports_tools": False,
        "supports_thinking": False,
        "supports_vision": False,
        "pricing_source": "unknown",
    }


def _catalog_entry(provider_type: str, model_id: str) -> Dict[str, Any]:
    """Return a merged catalog entry using the four-layer lookup.

    Precedence: static override (``_CATALOG``) > live upstream meta
    (``_LIVE_META``) > tier heuristic + provider default > safe default.
    The static override wins over live meta so hand-verified pricing
    isn't clobbered by zero-pricing from an upstream response — the API
    doesn't carry price data anyway, so the merge strategy is: take
    context/caps from live meta, take pricing from the layer that has
    it, with ``_CATALOG`` winning ties on display_name.
    """
    entry = dict(_default_entry(provider_type, model_id))
    static = _CATALOG.get((provider_type, model_id))
    live = _LIVE_META.get((provider_type, model_id))

    # Context / capabilities: prefer live meta (upstream source of truth),
    # fall back to static override, then default.
    for source in (live, static):
        if not source:
            continue
        for field_name in (
            "display_name",
            "context_window",
            "supports_tools",
            "supports_thinking",
            "supports_vision",
        ):
            if field_name in source and source[field_name]:
                entry[field_name] = source[field_name]

    # Pricing: static override → tier heuristic → provider-specific default.
    if static and ("input_per_m" in static or "output_per_m" in static):
        entry["input_per_m"] = float(static.get("input_per_m", 0.0))
        entry["output_per_m"] = float(static.get("output_per_m", 0.0))
        entry["pricing_source"] = "exact"
    elif provider_type == "ollama":
        entry["input_per_m"] = 0.0
        entry["output_per_m"] = 0.0
        entry["pricing_source"] = "zero"
    else:
        tier = _match_tier(provider_type, model_id)
        if tier is not None:
            entry["input_per_m"], entry["output_per_m"] = tier
            entry["pricing_source"] = "heuristic"
        else:
            entry["pricing_source"] = "unknown"
            logger.warning(
                "No catalog entry for %s/%s — defaulting cost to $0 and "
                "capabilities to false",
                provider_type,
                model_id,
            )

    return entry


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
    # One of: "exact" (from _CATALOG), "heuristic" (tier regex),
    # "zero" (ollama self-hosted), "unknown" (no data — treated as $0).
    # Logged at discovery time; frontend can use it to badge estimates.
    pricing_source: str = "exact"
    # True when the model was pinned to a component via ai_model_configs
    # but is no longer advertised by the upstream API. Kept in the UI
    # list so a user's saved selection doesn't silently disappear.
    deprecated: bool = False

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
            "pricing_source": self.pricing_source,
            "deprecated": self.deprecated,
        }


@dataclass(frozen=True)
class ComponentAssignment:
    component: str
    provider_id: str
    model_id: str
    settings: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-provider model list cache — no TTL
# ---------------------------------------------------------------------------
# Entries are valid until ``invalidate()`` is called (CRUD) or overwritten
# by ``sync_all_provider_models`` (scheduled refresh, manual refresh, or
# a lazy-sync on cold cache). The scheduled refresher — running every
# MODEL_CATALOG_REFRESH_INTERVAL_S (default 300s) — is the source of
# freshness. A time-based TTL here would just guarantee a latency spike
# on whichever page load falls on the expiry boundary.


class _ProviderModelCache:
    def __init__(self):
        self._entries: Dict[str, List[str]] = {}

    def get(self, provider_id: str) -> Optional[List[str]]:
        return self._entries.get(provider_id)

    def set(self, provider_id: str, models: List[str]) -> None:
        self._entries[provider_id] = models

    def invalidate(self, provider_id: Optional[str] = None) -> None:
        if provider_id is None:
            self._entries.clear()
        else:
            self._entries.pop(provider_id, None)


_MODEL_LIST_CACHE = _ProviderModelCache()


# Cold-boot fallback lists — used only when the live upstream API is
# unreachable at the exact moment a caller needs a list. Each entry is
# a small safe set; it is NOT the UI dropdown. Real model discovery
# runs through ``services.provider_model_discovery`` and populates the
# layered catalog above.

_FALLBACK_MODELS_BY_PROVIDER: Dict[str, Tuple[str, ...]] = {
    "anthropic": (
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5-20251001",
    ),
    "openai": (
        "gpt-4o",
        "gpt-4o-mini",
    ),
    "ollama": (),
}


# Extra model IDs that are NOT returned by the upstream /v1/models listing
# but are still callable. Unioned with the live list and rendered with
# ``deprecated=True`` so users get a visual cue that these aren't the
# preferred path. Default covers Anthropic's 3.x family which was pulled
# from the listing endpoint but remains routable. Override per deployment
# via env: ``ANTHROPIC_EXTRA_MODELS`` / ``OPENAI_EXTRA_MODELS`` (CSV).
# Set to empty string to disable for a provider.

_DEFAULT_EXTRA_MODELS: Dict[str, Tuple[str, ...]] = {
    "anthropic": (
        "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022",
        "claude-3-haiku-20240307",
    ),
    "openai": (),
    "ollama": (),
}


def get_extra_model_ids(provider_type: str) -> Tuple[str, ...]:
    """Return the extra model IDs for a provider type, honoring env
    overrides. Empty string disables; missing env falls back to defaults."""
    env_name = f"{provider_type.upper()}_EXTRA_MODELS"
    raw = os.getenv(env_name)
    if raw is None:
        return _DEFAULT_EXTRA_MODELS.get(provider_type, ())
    # Present but empty → explicitly disabled.
    parts = tuple(s.strip() for s in raw.split(",") if s.strip())
    return parts


# Tracks (provider_type, model_id) pairs added via the extras mechanism so
# ``list_available_models`` can tag them as deprecated in the UI response.
_EXTRA_IDS: set = set()


def _register_extras(provider_type: str, ids: Tuple[str, ...]) -> None:
    for mid in ids:
        _EXTRA_IDS.add((provider_type, mid))


def is_extra_model(provider_type: str, model_id: str) -> bool:
    return (provider_type, model_id) in _EXTRA_IDS


async def fetch_provider_models(row) -> List[str]:
    """Return the cached model list for a provider.

    Cache reader only — the sole writer is
    ``services.bifrost_admin.sync_all_provider_models`` which populates
    this cache at the same time it pushes to Bifrost. That shared-writer
    design is what prevents drift between the UI dropdown and Bifrost's
    allow-list.

    Cold start: if the cache is empty (e.g. startup sync hasn't completed
    or this row was added after the last scheduled refresh), trigger the
    canonical sync and re-read. If upstream is unreachable, fall back to
    the provider-type bootstrap list + extras so callers always get
    something to render.
    """
    cached = _MODEL_LIST_CACHE.get(row.provider_id)
    if cached is not None:
        return cached

    # Cold: run the canonical refresh. This populates the cache for every
    # active provider, so concurrent lazy-syncs for other rows are free.
    try:
        from services.bifrost_admin import sync_all_provider_models

        await sync_all_provider_models()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "fetch_provider_models(%s/%s): lazy sync failed: %s",
            row.provider_type,
            row.provider_id,
            exc,
        )

    cached = _MODEL_LIST_CACHE.get(row.provider_id)
    if cached is not None:
        return cached

    # Hard fallback: upstream unreachable AND sync didn't cache this row
    # (e.g. no API key configured). Populate with bootstrap + extras so
    # we don't keep retrying upstream on every dropdown open.
    provider_type = row.provider_type
    fallback = list(_FALLBACK_MODELS_BY_PROVIDER.get(provider_type, ()))
    extras = get_extra_model_ids(provider_type)
    _register_extras(provider_type, extras)
    for mid in extras:
        if mid not in fallback:
            fallback.append(mid)
    _MODEL_LIST_CACHE.set(row.provider_id, fallback)
    return fallback


# Backward-compat alias — kept so existing imports don't break.
# Prefer ``_FALLBACK_MODELS_BY_PROVIDER['anthropic']`` for new code.
ANTHROPIC_STATIC_MODELS: Tuple[str, ...] = _FALLBACK_MODELS_BY_PROVIDER["anthropic"]


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
        provider_id: str,
        provider_type: str,
        model_id: str,
        *,
        deprecated: bool = False,
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
            pricing_source=entry.get("pricing_source", "exact"),
            deprecated=deprecated,
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
        logged but never blocks the overall list. Models pinned via
        ``ai_model_configs`` that no longer appear in the live list are
        still returned with ``deprecated=True`` so users don't see their
        saved selection silently disappear.
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

        # Collect pinned (provider_id, model_id) pairs so we can keep
        # orphaned selections visible.
        pinned: Dict[Tuple[str, str], str] = {}
        for asn in self.get_all_assignments().values():
            pinned[(asn.provider_id, asn.model_id)] = (
                # provider_type is filled in below when the row exists.
                ""
            )

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

            provider_live: set = set()
            for mid in model_ids:
                key = (p.provider_id, mid)
                if key in seen:
                    continue
                seen.add(key)
                provider_live.add(mid)
                # Models added via the extras mechanism are deprecated —
                # they're still callable but upstream dropped them from
                # /v1/models, so the UI should badge them.
                is_deprecated = is_extra_model(p.provider_type, mid)
                out.append(
                    self.get_model_info(
                        provider_id=p.provider_id,
                        provider_type=p.provider_type,
                        model_id=mid,
                        deprecated=is_deprecated,
                    )
                )

            # Orphaned pins: a user has this provider+model pinned, but
            # upstream no longer advertises it. Preserve the entry so
            # the dropdown still renders the saved selection.
            for pin_provider_id, pin_model_id in pinned:
                if pin_provider_id != p.provider_id:
                    continue
                if pin_model_id in provider_live:
                    continue
                key = (pin_provider_id, pin_model_id)
                if key in seen:
                    continue
                seen.add(key)
                logger.info(
                    "Pinned model %s/%s is no longer advertised by upstream "
                    "— keeping in list as deprecated",
                    p.provider_type,
                    pin_model_id,
                )
                out.append(
                    self.get_model_info(
                        provider_id=p.provider_id,
                        provider_type=p.provider_type,
                        model_id=pin_model_id,
                        deprecated=True,
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
    the per-provider model-list cache + the discovery module's meta cache
    so the UI sees fresh data."""
    _MODEL_LIST_CACHE.invalidate(provider_id)
    # Provider-scoped live meta / discovery cache invalidation — best
    # effort. ``provider_id`` is a DB id, not a provider_type, so we can't
    # surgically drop a single entry; clear all meta + discovery cache
    # when any provider changes.
    try:
        from services import provider_model_discovery as discovery

        discovery.invalidate_cache()
    except Exception:  # noqa: BLE001
        pass
    clear_live_meta()
