"""Bifrost management API helper.

Bifrost exposes a REST admin API at ``${BIFROST_URL}/api/providers/{name}``
that lets us update provider keys at runtime without a container restart.
This module is the one place the backend talks to that API, so the flow
is: user edits a key in the UI → ``llm_providers`` endpoint writes to the
secrets manager → this module pushes the new value to Bifrost → Bifrost
uses it for subsequent requests.

The previous architecture had Bifrost read ``env.ANTHROPIC_API_KEY`` from
its container env, which diverged from whatever the UI had written to the
secrets manager under ``llm_provider_<id>_api_key``. Pushing via the API
keeps a single source of truth in the secrets manager.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5.0

# In-flight future used to coalesce concurrent ``sync_all_provider_models``
# calls. If a sync is running and a second caller arrives (e.g. a cold
# dropdown lazy-sync landing during the scheduled refresher's iteration),
# the second caller awaits the same future instead of issuing a duplicate
# round of upstream fetches. None when idle.
_sync_in_flight: Optional["asyncio.Future[Dict[str, Any]]"] = None


def _bifrost_base_url() -> str:
    return os.getenv("BIFROST_URL", "http://localhost:8080").rstrip("/")


def _get_provider(name: str, client: httpx.Client) -> Optional[Dict[str, Any]]:
    try:
        r = client.get(
            f"{_bifrost_base_url()}/api/providers/{name}", timeout=_DEFAULT_TIMEOUT
        )
        if r.status_code == 404:
            logger.debug("Bifrost: provider %s not configured", name)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("Bifrost: could not fetch provider %s: %s", name, e)
        return None


def push_provider_key(provider_name: str, key_value: str) -> bool:
    """Update the first configured key on ``provider_name`` with ``key_value``.

    We take the current provider document, replace the key value with a
    literal (``from_env: false``), and PUT it back. This is idempotent —
    pushing the same value twice is a no-op from Anthropic's perspective.

    Returns True on success. Any failure is logged and returns False so
    the caller's CRUD flow never breaks on a Bifrost hiccup.
    """
    if not provider_name:
        return False
    with httpx.Client() as client:
        prov = _get_provider(provider_name, client)
        if prov is None:
            return False
        keys = prov.get("keys") or []
        if not keys:
            logger.warning(
                "Bifrost: provider %s has no keys slot to update", provider_name
            )
            return False
        # Update the first key. Bifrost's current config seeds exactly one
        # key per provider; multi-key rotation is a separate feature.
        keys[0]["value"] = {
            "value": key_value,
            "env_var": "",
            "from_env": False,
        }
        try:
            r = client.put(
                f"{_bifrost_base_url()}/api/providers/{provider_name}",
                json=prov,
                timeout=_DEFAULT_TIMEOUT,
            )
            if r.status_code >= 400:
                logger.warning(
                    "Bifrost: PUT /api/providers/%s returned %s: %s",
                    provider_name,
                    r.status_code,
                    r.text[:200],
                )
                return False
            logger.info("Bifrost: pushed updated key for provider %s", provider_name)
            return True
        except Exception as e:
            logger.warning(
                "Bifrost: PUT /api/providers/%s failed: %s", provider_name, e
            )
            return False


def sync_all_provider_keys() -> Dict[str, bool]:
    """Push every DB-configured provider's current secret value to Bifrost.

    Run on backend startup so Bifrost picks up whatever is in the secrets
    store regardless of how it was started or whether its container was
    recreated. Best-effort — returns a per-provider dict of success flags.
    """
    # Deferred imports to keep this module import-cheap for code that only
    # needs ``push_provider_key`` (e.g. llm_providers.py).
    from backend.secrets_manager import get_secret
    from database.connection import get_db_manager
    from database.models import LLMProviderConfig

    results: Dict[str, bool] = {}
    db_manager = get_db_manager()
    if db_manager._engine is None:
        db_manager.initialize()
    with db_manager.session_scope() as session:
        rows = (
            session.query(LLMProviderConfig)
            .filter(
                LLMProviderConfig.is_active.is_(True),
            )
            .all()
        )
        for row in rows:
            if not row.api_key_ref:
                continue
            value = get_secret(row.api_key_ref)
            if not value:
                logger.debug(
                    "Bifrost sync: no value in secrets store for %s (ref=%s)",
                    row.provider_id,
                    row.api_key_ref,
                )
                results[row.provider_id] = False
                continue
            results[row.provider_id] = push_provider_key(row.provider_type, value)
    if results:
        ok = sum(1 for v in results.values() if v)
        logger.info("Bifrost sync: pushed %d/%d provider keys", ok, len(results))
    return results


def sync_provider_models(provider_type: str, model_ids: list[str]) -> bool:
    """Update Bifrost's allow-list of routable models for ``provider_type``.

    GETs the provider document, replaces ``keys[0].models`` with
    ``model_ids``, and PUTs the result back. Empty lists are skipped —
    wiping the allow-list to ``[]`` would cause Bifrost to reject every
    subsequent LLM call for that provider, which we never want just
    because an upstream API had a momentary hiccup.
    """
    if not provider_type:
        return False
    if not model_ids:
        logger.info(
            "Bifrost sync: skipping empty model list for provider %s "
            "(refusing to wipe allow-list)",
            provider_type,
        )
        return False
    # Normalize + dedupe while preserving order.
    seen: set = set()
    normalized: list[str] = []
    for mid in model_ids:
        if not mid or mid in seen:
            continue
        seen.add(mid)
        normalized.append(mid)

    with httpx.Client() as client:
        prov = _get_provider(provider_type, client)
        if prov is None:
            return False
        keys = prov.get("keys") or []
        if not keys:
            logger.warning(
                "Bifrost: provider %s has no keys slot to update",
                provider_type,
            )
            return False
        keys[0]["models"] = normalized
        try:
            r = client.put(
                f"{_bifrost_base_url()}/api/providers/{provider_type}",
                json=prov,
                timeout=_DEFAULT_TIMEOUT,
            )
            if r.status_code >= 400:
                logger.warning(
                    "Bifrost: PUT /api/providers/%s (models) returned %s: %s",
                    provider_type,
                    r.status_code,
                    r.text[:200],
                )
                return False
            logger.info(
                "Bifrost: synced %d models for provider %s",
                len(normalized),
                provider_type,
            )
            return True
        except Exception as e:
            logger.warning(
                "Bifrost: PUT /api/providers/%s (models) failed: %s",
                provider_type,
                e,
            )
            return False


async def sync_all_provider_models() -> Dict[str, Any]:
    """Canonical refresh for every active LLM provider.

    Single source of truth — called at startup, on a schedule, from the
    refresh endpoints, and lazily on a dropdown cache miss. One call
    does everything:

    1. Fetches each provider's live upstream catalog via
       ``services.provider_model_discovery``.
    2. Applies the configured extras (IDs upstream dropped from
       /v1/models but that still route — e.g. Claude 3.x).
    3. Populates ``_MODEL_LIST_CACHE[provider_id]`` in
       ``services.model_registry`` so the UI dropdown reads the same
       list the sync just computed.
    4. Unions per-provider-type across rows and PUTs that to Bifrost's
       allow-list via the admin API, so LLM traffic routes for every
       model the dropdown shows.

    Because all three surfaces (dropdown cache, live-meta cache, Bifrost
    allow-list) are written in the same pass, they cannot drift.

    Concurrent calls are coalesced — if a sync is already running (e.g.
    the scheduled refresher kicked off at the same time as a dropdown
    cold-load), the second caller awaits the same future rather than
    issuing a duplicate round of upstream fetches.

    Best-effort — never raises. Returns a dict with per-provider-type
    Bifrost sync flags under ``bifrost`` and the computed per-row model
    lists under ``models_by_provider`` for observability.
    """
    global _sync_in_flight
    if _sync_in_flight is not None and not _sync_in_flight.done():
        logger.debug("sync_all_provider_models: joining in-flight sync")
        return await _sync_in_flight

    loop = asyncio.get_running_loop()
    _sync_in_flight = loop.create_future()
    try:
        result = await _do_sync_all_provider_models()
        _sync_in_flight.set_result(result)
        return result
    except Exception as exc:
        _sync_in_flight.set_exception(exc)
        raise
    finally:
        # Release the slot so the next scheduled tick or CRUD event can
        # start a fresh sync.
        _sync_in_flight = None


async def _do_sync_all_provider_models() -> Dict[str, Any]:
    # Deferred imports to keep module load cheap.
    from database.connection import get_db_manager
    from database.models import LLMProviderConfig
    from services import provider_model_discovery as discovery
    from services.model_registry import (
        _FALLBACK_MODELS_BY_PROVIDER,
        _MODEL_LIST_CACHE,
        _register_extras,
        get_extra_model_ids,
        record_live_meta,
    )

    db_manager = get_db_manager()
    if db_manager._engine is None:
        db_manager.initialize()

    # Group active providers by type and collect the rows we need to
    # fetch (we don't hold the session open across awaits).
    rows_by_type: Dict[str, list] = {}
    with db_manager.session_scope() as session:
        rows = (
            session.query(LLMProviderConfig)
            .filter(
                LLMProviderConfig.is_active.is_(True),
            )
            .all()
        )
        for row in rows:
            # Detach enough state from the row so we can use it after the
            # session closes. The ORM row becomes unusable post-scope.
            rows_by_type.setdefault(row.provider_type, []).append(
                {
                    "provider_id": row.provider_id,
                    "provider_type": row.provider_type,
                    "base_url": row.base_url,
                    "api_key_ref": row.api_key_ref,
                    "config": dict(row.config or {}),
                }
            )

    bifrost_results: Dict[str, bool] = {}
    per_row_models: Dict[str, List[str]] = {}

    for provider_type, provider_rows in rows_by_type.items():
        # Extras are per-provider-type; apply to every row of this type.
        extras = get_extra_model_ids(provider_type)
        _register_extras(provider_type, extras)

        type_union: List[str] = []
        type_seen: set = set()

        for row_dict in provider_rows:
            row_ids: List[str] = []
            row_seen: set = set()
            upstream_ok = False

            try:
                meta = await _fetch_meta_for_row(row_dict, discovery)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "sync_all_provider_models: discovery failed for %s (%s): %s",
                    row_dict["provider_id"],
                    provider_type,
                    exc,
                )
                meta = None

            if meta is not None:
                upstream_ok = True
                record_live_meta(provider_type, meta)
                for m in meta:
                    if m.id in row_seen:
                        continue
                    row_seen.add(m.id)
                    row_ids.append(m.id)

            # Upstream failed: union the bootstrap list so the dropdown
            # isn't empty while still carrying the extras below.
            if not upstream_ok:
                for mid in _FALLBACK_MODELS_BY_PROVIDER.get(provider_type, ()):
                    if mid not in row_seen:
                        row_seen.add(mid)
                        row_ids.append(mid)

            # Extras are unioned into the row list so the dropdown shows
            # them and the Bifrost allow-list contains them — same list,
            # same source.
            for mid in extras:
                if mid not in row_seen:
                    row_seen.add(mid)
                    row_ids.append(mid)

            # Single-writer: populate the dropdown cache with this row's
            # list. ``fetch_provider_models`` reads this same key.
            _MODEL_LIST_CACHE.set(row_dict["provider_id"], row_ids)
            per_row_models[row_dict["provider_id"]] = row_ids

            # Contribute to the per-type union for Bifrost.
            for mid in row_ids:
                if mid in type_seen:
                    continue
                type_seen.add(mid)
                type_union.append(mid)

        if not type_union:
            # Preserve bootstrap: don't overwrite Bifrost's allow-list
            # with an empty list if every row failed and there are no
            # extras or fallback.
            bifrost_results[provider_type] = False
            continue

        bifrost_results[provider_type] = sync_provider_models(provider_type, type_union)

    if bifrost_results:
        ok = sum(1 for v in bifrost_results.values() if v)
        logger.info(
            "Model catalog sync: pushed model lists for %d/%d provider types",
            ok,
            len(bifrost_results),
        )

    return {
        "bifrost": bifrost_results,
        "models_by_provider": per_row_models,
    }


async def _fetch_meta_for_row(row_dict: Dict[str, Any], discovery) -> Optional[list]:
    """Call the appropriate discovery function for one provider row.

    Returns ``None`` when the row isn't usable (e.g. no API key). The
    caller logs and skips.
    """
    from backend.secrets_manager import get_secret

    provider_type = row_dict["provider_type"]
    base_url = row_dict.get("base_url")
    api_key_ref = row_dict.get("api_key_ref")
    config = row_dict.get("config") or {}

    def _resolve_key() -> Optional[str]:
        if api_key_ref:
            try:
                val = get_secret(api_key_ref)
                if val:
                    return val
            except Exception as exc:  # noqa: BLE001
                logger.debug("secret lookup for %s failed: %s", api_key_ref, exc)
        if provider_type == "anthropic":
            return os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
        if provider_type == "openai":
            return os.getenv("OPENAI_API_KEY")
        return None

    if provider_type == "anthropic":
        key = _resolve_key()
        if not key:
            logger.info(
                "Bifrost sync: no Anthropic key available for %s — skipping",
                row_dict["provider_id"],
            )
            return None
        return await discovery.fetch_anthropic_models(key)

    if provider_type == "openai":
        key = _resolve_key()
        if not key:
            logger.info(
                "Bifrost sync: no OpenAI key available for %s — skipping",
                row_dict["provider_id"],
            )
            return None
        return await discovery.fetch_openai_models(
            key,
            base_url=base_url,
            organization=config.get("organization"),
        )

    if provider_type == "ollama":
        return await discovery.fetch_ollama_models(base_url)

    logger.debug("Bifrost sync: unsupported provider_type %s", provider_type)
    return None
