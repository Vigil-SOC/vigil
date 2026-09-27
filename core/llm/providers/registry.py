"""Model registry — aggregates model metadata and resolves per-component assignments.

Sits on top of the multi-provider layer introduced in #88:
  - Reads `ai_model_configs` (component → provider+model) for assignments.
  - Reads `llm_provider_configs` for provider info.
  - Live-queries providers for their current model lists via
    ``core.llm.providers.discovery`` (Anthropic /v1/models, OpenAI
    /v1/models, Ollama /api/tags + /api/show), with a short TTL cache.
  - Owns the cost/capability catalog, read entirely from live meta: provider
    discovery for capabilities, the gateway's datasheet for rates.

The registry is intentionally decoupled from the DB hot path: all DB access
goes through short-lived sessions that are closed before returning.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.llm.providers.discovery import is_embedding_model_id
from core.llm.router.router import get_default_provider_spec

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pricing observability (#184 acceptance: unknown pricing must not be silent)
# ---------------------------------------------------------------------------

# OTEL counter — fires when a (provider_type, model_id) pair has no exact
# or zero pricing entry. Lazy-init so import order doesn't matter
# and so tests that don't exercise telemetry stay fast.
_pricing_unknown_counter = None


def _record_pricing_unknown(provider_type: str, model_id: str) -> None:
    """Record an unknown-pricing event on the OTEL counter.

    No-ops if the OTEL bridge isn't installed (e.g. during unit tests that
    haven't set up telemetry). The structured log line at the call site
    handles the human-readable signal regardless.
    """
    global _pricing_unknown_counter
    try:
        if _pricing_unknown_counter is None:
            from core.telemetry import get_meter

            meter = get_meter("vigil.cost")
            _pricing_unknown_counter = meter.create_counter(
                name="vigil_llm_cost_pricing_unknown_total",
                description=(
                    "LLM calls priced against an unknown model — recorded as "
                    "unpriced (NULL cost). The gateway datasheet holds no rate "
                    "for the (provider, model) pair; set one as a gateway "
                    "pricing override."
                ),
                unit="1",
            )
        _pricing_unknown_counter.add(
            1,
            {
                "provider_type": provider_type or "unknown",
                "model_id": model_id or "unknown",
            },
        )
    except Exception:
        # Telemetry must never break cost math.
        pass


# ---------------------------------------------------------------------------
# Component enum (mirrors ai_model_configs.component values)
# ---------------------------------------------------------------------------

COMPONENTS: Tuple[str, ...] = (
    "chat_default",
    "triage",
    "investigation",
    "summarization",
    "reporting",
)


def is_valid_component(name: str) -> bool:
    return name in COMPONENTS


# ---------------------------------------------------------------------------
# Catalog — live meta only
# ---------------------------------------------------------------------------
#
# Everything here is read from ``_LIVE_META`` below, which two readers fill:
#   - ``core.llm.providers.discovery`` (display name, context window and
#     capability flags from each provider's own /models endpoint), and
#   - the gateway's ``/api/models/details`` datasheet
#     (``core.llm.bifrost.admin.fetch_catalogue_models``), which publishes all
#     four per-token rates per model, operator pricing overrides included.
# Vigil keeps no rate card of its own: a price is set as a gateway pricing
# override, and a model the gateway does not price is "unknown", never free.

# The provider types Vigil can configure. Also what a ``provider/model`` id may
# name, since that is the gateway's own wire form.
VALID_PROVIDER_TYPES = frozenset({"anthropic", "openai", "ollama", "vertex"})


def infer_provider_type(model_id: str) -> str:
    """Best-effort provider inference from a bare model id.

    ``LLMInteractionLog`` only stores ``model``, not ``provider_type``,
    so analytics needs to infer the provider when grouping by model to
    look up pricing. This is good enough for cost-source badging — if it
    misclassifies, the worst case is the badge shows ``"unknown"`` which
    is exactly what we want a reader to see.

    A ``provider/model`` id is Bifrost's own wire form and names the
    provider outright, so it is read rather than guessed at.
    """
    if not model_id:
        return "unknown"
    named, _, rest = model_id.partition("/")
    if rest and named.lower() in VALID_PROVIDER_TYPES:
        return named.lower()
    mid = model_id.lower()
    if mid.startswith("claude-"):
        return "anthropic"
    if mid.startswith(("gpt-", "o1", "o3", "o4", "text-embedding")):
        return "openai"
    if mid.startswith("gemini"):
        return "gemini"
    # Ollama has no canonical prefix; the remaining path is a soft match
    # against common open-weight names.
    if any(s in mid for s in ("llama", "mistral", "mixtral", "qwen", "gemma")):
        return "ollama"
    return "unknown"


# ---------------------------------------------------------------------------
# Live-meta cache — populated by the discovery module
# ---------------------------------------------------------------------------

# Maps (provider_type, model_id) → catalog-shape dict with the meta we got
# from upstream: capabilities from discovery, rates from the gateway datasheet.
# Written by ``record_live_meta``; cleared when the discovery cache is
# invalidated. Every process that prices a call fills its own copy
# (``core.llm.bifrost.admin.refresh_gateway_rates``).

_LIVE_META: Dict[Tuple[str, str], Dict[str, Any]] = {}

# ``ModelMeta`` rate attribute → per-token key held in ``_LIVE_META``.
_RATE_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_cost_per_token",
    "cache_write_cost_per_token",
)


def record_live_meta(
    provider_type: str, meta_list: List[Any], *, rates_only: bool = False
) -> None:
    """Merge a list of ``ModelMeta`` into the live catalog.

    Discovery and the datasheet both write here, in either order, so neither
    wipes the other: a record that states no rates leaves stored rates alone,
    and ``rates_only`` (a datasheet read for its prices) leaves display name,
    context and capabilities alone.
    """
    fetched_at = datetime.now(timezone.utc).isoformat()
    for m in meta_list:
        entry = _LIVE_META.setdefault((provider_type, m.id), {})
        if not rates_only:
            caps = getattr(m, "capabilities", {}) or {}
            entry.update(
                display_name=getattr(m, "display_name", m.id),
                context_window=int(getattr(m, "context_window", 0) or 0),
                supports_tools=bool(caps.get("supports_tools", False)),
                supports_thinking=bool(caps.get("supports_thinking", False)),
                supports_vision=bool(caps.get("supports_vision", False)),
                is_embedding=bool(caps.get("is_embedding", False)),
            )
        rates = {f: getattr(m, f, None) for f in _RATE_FIELDS}
        if any(v is not None for v in rates.values()):
            entry.update(rates, rates_fetched_at=fetched_at)


def clear_live_meta(provider_type: Optional[str] = None) -> None:
    if provider_type is None:
        _LIVE_META.clear()
        return
    for key in list(_LIVE_META.keys()):
        if key[0] == provider_type:
            _LIVE_META.pop(key, None)


def _clear_discovered_meta() -> None:
    """Drop what discovery recorded but keep the gateway's rates, which no
    provider-config change invalidates — dropping them would record every call
    until the next sync as unpriced."""
    for key, entry in list(_LIVE_META.items()):
        if "rates_fetched_at" not in entry:
            del _LIVE_META[key]
            continue
        _LIVE_META[key] = {f: entry.get(f) for f in (*_RATE_FIELDS, "rates_fetched_at")}


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def _default_entry(provider_type: str, model_id: str) -> Dict[str, Any]:
    return {
        "display_name": model_id,
        "context_window": 0,
        # Per-token USD.
        "input": 0.0,
        "output": 0.0,
        "cache_read": 0.0,
        "cache_write": 0.0,
        "supports_tools": False,
        "supports_thinking": False,
        "supports_vision": False,
        "is_embedding": False,
        "pricing_source": "unknown",
        "rates_fetched_at": None,
    }


def _catalog_entry(provider_type: str, model_id: str) -> Dict[str, Any]:
    """Return the catalog entry for one model, priced from live meta only.

    Input and output rates present → ``exact`` (``zero`` when every rate is
    zero); no gateway rates for an ``ollama`` model → ``zero``, since it is
    self-hosted; anything else → ``unknown``. A missing cache rate is charged
    at the input rate; a missing input or output rate is never read as zero.
    """
    entry = dict(_default_entry(provider_type, model_id))
    live = _LIVE_META.get((provider_type, model_id)) or {}

    for field_name in (
        "display_name",
        "context_window",
        "supports_tools",
        "supports_thinking",
        "supports_vision",
        "is_embedding",
    ):
        if live.get(field_name):
            entry[field_name] = live[field_name]

    in_rate = live.get("input_cost_per_token")
    out_rate = live.get("output_cost_per_token")
    if in_rate is not None and out_rate is not None:
        read = live.get("cache_read_cost_per_token")
        write = live.get("cache_write_cost_per_token")
        rates = {
            "input": float(in_rate),
            "output": float(out_rate),
            "cache_read": float(in_rate if read is None else read),
            "cache_write": float(in_rate if write is None else write),
        }
        entry.update(rates, rates_fetched_at=live.get("rates_fetched_at"))
        entry["pricing_source"] = "zero" if not any(rates.values()) else "exact"
    elif provider_type == "ollama" and "rates_fetched_at" not in live:
        entry["pricing_source"] = "zero"
    else:
        logger.warning(
            "No gateway rate for %s/%s — its calls are recorded as unpriced",
            provider_type,
            model_id,
        )
        _record_pricing_unknown(provider_type, model_id)

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
    # One of: "exact" (gateway datasheet rates), "zero" (all rates zero, or
    # self-hosted ollama), "unknown" (no data — calls recorded as unpriced).
    # Logged at discovery time; frontend can use it to badge estimates.
    pricing_source: str = "exact"
    # True when the model was pinned to a component via ai_model_configs
    # but is no longer advertised by the upstream API. Kept in the UI
    # list so a user's saved selection doesn't silently disappear.
    deprecated: bool = False
    # True for embedding-only models (e.g. nomic-embed-text). They stay in
    # the registry (analytics / future config) but are filtered out of the
    # chat model picker, since you can't hold a conversation with them.
    is_embedding: bool = False

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
            "is_embedding": self.is_embedding,
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


_MODEL_LIST_CACHE: Dict[str, List[str]] = {}

# Which ``_MODEL_LIST_CACHE`` entries are a real catalogue rather than the
# bootstrap floor below, which must never be used to rule a model out.
_LIVE_CATALOGUES: set = set()


def catalogue_of(provider_id: str) -> Optional[List[str]]:
    """The models ``provider_id`` is known to serve, or None if not known."""
    if provider_id not in _LIVE_CATALOGUES:
        return None
    return _MODEL_LIST_CACHE.get(provider_id) or None


# Cold-boot fallback lists — used only when the live upstream API is
# unreachable at the exact moment a caller needs a list. Each entry is
# a small safe set; it is NOT the UI dropdown. Real model discovery
# runs through ``core.llm.providers.discovery`` and populates the
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
        # Legacy 4.x IDs kept routable for users with persisted
        # localStorage settings still referencing them. Tagged deprecated
        # in the UI dropdown via the extras-tracking mechanism below.
        "claude-sonnet-4-20250514",
        "claude-opus-4-20250514",
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
    # Provider-derived name, so this stays a dynamic lookup rather than a field.
    raw = os.getenv(env_name)  # noqa: ENV001
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


def is_chat_model(provider_type: str, model_id: str) -> bool:
    """False for embedding-only models, which can't hold a chat.

    Either signal rules a model out: the live ``is_embedding`` capability
    flag when discovery recorded one, or the name heuristic (which also
    covers ids with no live meta). Same test the chat picker has always
    applied. Reads ``_LIVE_META`` directly rather than through
    ``_catalog_entry`` so unknown ids don't trip the pricing warning.
    """
    live = _LIVE_META.get((provider_type, model_id))
    if live and live.get("is_embedding"):
        return False
    return not is_embedding_model_id(model_id)


def _chat_models(provider_type: str, model_ids: List[str]) -> List[str]:
    """New list with embedding-only ids dropped; never mutates the input."""
    return [mid for mid in model_ids if is_chat_model(provider_type, mid)]


async def fetch_provider_models(row) -> List[str]:
    """Return the cached model list for a provider, minus embedding models.

    Cache reader only — the sole writer is
    ``core.llm.bifrost.admin.sync_all_provider_models`` which populates
    this cache at the same time it pushes to Bifrost. That shared-writer
    design is what prevents drift between the UI dropdown and Bifrost's
    allow-list.

    Every caller feeds a chat-model picker, so embedding-only models are
    filtered out here (#1004). The filter is applied to the returned copy
    only: ``_MODEL_LIST_CACHE`` keeps the full catalogue because
    ``catalogue_of`` and Bifrost's allow-list must still see it.

    Cold start: if the cache is empty (e.g. startup sync hasn't completed
    or this row was added after the last scheduled refresh), trigger the
    canonical sync and re-read. If upstream is unreachable, fall back to
    the provider-type bootstrap list + extras so callers always get
    something to render.
    """
    cached = _MODEL_LIST_CACHE.get(row.provider_id)
    if cached is not None:
        return _chat_models(row.provider_type, cached)

    # Cold: run the canonical refresh. This populates the cache for every
    # active provider, so concurrent lazy-syncs for other rows are free.
    try:
        from core.llm.bifrost.admin import sync_all_provider_models

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
        return _chat_models(row.provider_type, cached)

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
    _MODEL_LIST_CACHE[row.provider_id] = fallback
    _LIVE_CATALOGUES.discard(row.provider_id)
    return _chat_models(provider_type, fallback)


# Backward-compat alias — kept so existing imports don't break.
# Prefer ``_FALLBACK_MODELS_BY_PROVIDER['anthropic']`` for new code.
ANTHROPIC_STATIC_MODELS: Tuple[str, ...] = _FALLBACK_MODELS_BY_PROVIDER["anthropic"]


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

        ``(0.0, 0.0)`` for an unpriced model — read ``get_pricing_source``
        (or use ``get_call_pricing``) before trusting a zero.
        """
        entry = _catalog_entry(provider_type, model_id)
        return (entry["input"], entry["output"])

    @staticmethod
    def get_cache_rates(model_id: str, provider_type: str) -> Tuple[float, float]:
        """Return ``(cache_read_per_token, cache_creation_per_token)`` in USD,
        as the gateway states them per model."""
        entry = _catalog_entry(provider_type, model_id)
        return (entry["cache_read"], entry["cache_write"])

    @staticmethod
    def get_pricing_source(model_id: str, provider_type: str) -> str:
        """Return how pricing resolved: ``"exact"``, ``"zero"`` or ``"unknown"``.

        Lets callers (analytics, UI badges) tell a $0 row that's accurate
        from a model nobody could price.
        """
        return _catalog_entry(provider_type, model_id).get("pricing_source", "unknown")

    @staticmethod
    def get_call_pricing(
        model_id: str, provider_type: str
    ) -> Tuple[str, Tuple[float, float, float, float]]:
        """``(pricing_source, (input, output, cache_read, cache_creation))``.

        Per-token USD from a single catalog read, so pricing one call fires
        the pricing-unknown counter at most once.
        """
        entry = _catalog_entry(provider_type, model_id)
        return entry.get("pricing_source", "unknown"), (
            entry["input"],
            entry["output"],
            entry["cache_read"],
            entry["cache_write"],
        )

    @staticmethod
    def get_rates(model_id: str, provider_type: str) -> Dict[str, Any]:
        """All four per-token rates, the pricing source and when the rates
        were fetched from the gateway (ISO timestamp, None if never)."""
        entry = _catalog_entry(provider_type, model_id)
        return {
            k: entry[k]
            for k in (
                "input",
                "output",
                "cache_read",
                "cache_write",
                "pricing_source",
                "rates_fetched_at",
            )
        }

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
            input_cost_per_1k=entry["input"] * 1000,
            output_cost_per_1k=entry["output"] * 1000,
            supports_tools=entry["supports_tools"],
            supports_thinking=entry["supports_thinking"],
            supports_vision=entry["supports_vision"],
            pricing_source=entry.get("pricing_source", "exact"),
            deprecated=deprecated,
            is_embedding=bool(entry.get("is_embedding", False)),
        )

    # ---- assignments -----------------------------------------------------

    def get_all_assignments(self) -> Dict[str, ComponentAssignment]:
        """Return every configured assignment keyed by component."""
        try:
            from core.storage.connection import get_db_session
            from core.storage.models import AIModelConfig
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
          → the default active provider of any type (get_default_provider_spec)
            and its default_model

        Returns None when no DB is reachable or no provider is active.
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

        spec = get_default_provider_spec()
        if spec is not None:
            return (spec.provider_id, spec.default_model)
        return None

    # ---- provider helpers ------------------------------------------------

    def _default_anthropic_provider(self) -> Optional[Dict[str, str]]:
        try:
            from core.storage.connection import get_db_session
            from core.storage.models import LLMProviderConfig
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

    def _active_providers(self) -> List:
        """Return active ``LLMProviderConfig`` rows.

        Isolated (and overridable) so the aggregation logic in
        ``list_available_models`` / ``fallback_models`` can be unit-tested
        without a live Postgres.
        """
        try:
            from core.storage.connection import get_db_session
            from core.storage.models import LLMProviderConfig
        except Exception as exc:  # noqa: BLE001
            logger.debug("_active_providers: DB unreachable: %s", exc)
            return []

        session = get_db_session()
        try:
            return (
                session.query(LLMProviderConfig)
                .filter(LLMProviderConfig.is_active.is_(True))
                .all()
            )
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
        providers = self._active_providers()

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

            # An active provider that discovers no models (e.g. an Ollama
            # endpoint unreachable at sync time — its bootstrap list is
            # empty) would otherwise contribute nothing, collapsing the
            # aggregate to whatever other providers return (often just
            # Anthropic). Floor it to the configured default_model so the
            # provider is always represented in the picker (#409).
            if not model_ids and getattr(p, "default_model", None):
                logger.info(
                    "Provider %s (%s) discovered no models — flooring to "
                    "default_model %s so it still appears in the picker",
                    p.provider_id,
                    p.provider_type,
                    p.default_model,
                )
                model_ids = [p.default_model]

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
                    "Pinned model %s/%s is not in the provider's chat-model "
                    "list — keeping in list as deprecated",
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

    async def fallback_models(self) -> List[ModelInfo]:
        """Provider-aware bootstrap list for the picker.

        Used when live discovery yields nothing at all. Reflects the
        *configured* providers (their bootstrap lists, extras and
        ``default_model``) instead of hardcoding Claude, so an Ollama-only
        instance never shows Anthropic models it can't call (#409). Returns
        ``[]`` when no provider is configured — the caller then applies its
        own last-resort default.
        """
        out: List[ModelInfo] = []
        seen: set = set()
        for p in self._active_providers():
            ids = list(_FALLBACK_MODELS_BY_PROVIDER.get(p.provider_type, ()))
            for mid in get_extra_model_ids(p.provider_type):
                if mid not in ids:
                    ids.append(mid)
            default_model = getattr(p, "default_model", None)
            if default_model and default_model not in ids:
                ids.insert(0, default_model)
            for mid in ids:
                key = (p.provider_id, mid)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    self.get_model_info(
                        provider_id=p.provider_id,
                        provider_type=p.provider_type,
                        model_id=mid,
                        deprecated=is_extra_model(p.provider_type, mid),
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
    if provider_id is None:
        _MODEL_LIST_CACHE.clear()
        _LIVE_CATALOGUES.clear()
    else:
        _MODEL_LIST_CACHE.pop(provider_id, None)
        _LIVE_CATALOGUES.discard(provider_id)
    # Provider-scoped live meta / discovery cache invalidation — best
    # effort. ``provider_id`` is a DB id, not a provider_type, so we can't
    # surgically drop a single entry; clear all discovered meta + discovery
    # cache when any provider changes.
    try:
        from core.llm.providers import discovery

        discovery.invalidate_cache()
    except Exception:  # noqa: BLE001
        pass
    _clear_discovered_meta()
