"""Bifrost management API helper.

Bifrost exposes a REST admin API that lets us update provider credentials at
runtime without a container restart. This module is the one place the backend
talks to that API, so the flow is: user edits a key in the UI →
``llm_providers`` endpoint writes to the secrets manager → this module pushes
the new value to Bifrost → Bifrost uses it for subsequent requests.

The alternative would be letting Bifrost read ``env.ANTHROPIC_API_KEY`` from
its container env, which diverges from whatever the UI wrote to the secrets
manager under ``llm_provider_<id>_api_key``. Pushing via the API keeps a
single source of truth in the secrets manager, and is why the seeded
``config.json`` key is an empty placeholder rather than a real credential.

Three properties of that API shape the code below, all learned the hard way:

* **Keys are a subresource.** They live at ``/api/providers/{name}/keys``,
  *not* on the ``/api/providers/{name}`` document — which returns no ``keys``
  field at all.
* **Secrets are masked on read** (``sk-a****key``) and a write that echoes the
  mask is accepted with a 200, storing the mask as the credential. So values
  are always re-read from the secrets store, never round-tripped.
* **A key must carry a non-empty value.** There is no models-only update, and
  clearing a credential means deleting the key rather than blanking it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 5.0

# In-flight future used to coalesce concurrent ``sync_all_provider_models``
# calls. If a sync is running and a second caller arrives (e.g. a cold
# dropdown lazy-sync landing during the scheduled refresher's iteration),
# the second caller awaits the same future instead of issuing a duplicate
# round of upstream fetches. None when idle.
_sync_in_flight: Optional["asyncio.Future[Dict[str, Any]]"] = None


def _bifrost_base_url() -> str:
    return get_settings().bifrost_url.rstrip("/")


# Providers whose Bifrost key is not an API secret we own. Ollama's key holds
# a URL under ``ollama_key_config`` with an empty ``value``, and its allow-list
# is the static wildcard, so there is nothing for us to push.
#
# Vertex for the same reason: its credential is a service-account file, and
# ``discovery.py`` has no fetcher for its catalog, so a managed sync would push an
# allow-list refusing every model. The seeded wildcard in the Bifrost config is it.
_UNMANAGED_PROVIDER_TYPES = frozenset({"ollama", "vertex"})

# Read-only/derived fields Bifrost returns but rejects or ignores on write.
# ``value`` is dropped separately — see the module docstring on masking.
_KEY_READBACK_ONLY = frozenset({"id", "config_hash", "status", "description"})

# Key ``status`` values observed from Bifrost that do not indicate a problem:
# "success" means it listed models with the credential, "unknown" means it has
# not checked yet. Anything else (e.g. "list_models_failed") gets a warning —
# an unrecognized value is worth one line of noise, whereas guessing at extra
# members here would silence the exact failure this check exists to catch.
_KEY_STATUS_OK = frozenset({"success", "unknown"})


def _keys_url(provider_name: str, key_id: Optional[str] = None) -> str:
    base = f"{_bifrost_base_url()}/api/providers/{provider_name}/keys"
    return f"{base}/{key_id}" if key_id else base


def _get_provider_keys(
    name: str, client: httpx.Client
) -> Optional[List[Dict[str, Any]]]:
    """Return ``name``'s configured keys, or None if the provider is absent.

    An empty list means "provider exists, no keys yet" — distinct from None,
    which is why callers branch on ``is None`` rather than truthiness.
    """
    try:
        r = client.get(_keys_url(name), timeout=_DEFAULT_TIMEOUT)
        if r.status_code == 404:
            logger.debug("Bifrost: provider %s not configured", name)
            return None
        r.raise_for_status()
        return r.json().get("keys") or []
    except Exception as e:
        logger.warning("Bifrost: could not fetch keys for provider %s: %s", name, e)
        return None


def _log_key_health(provider_name: str, payload: Dict[str, Any]) -> None:
    """Log Bifrost's own verdict on a key it just accepted.

    Bifrost validates the credential upstream on write and reports the result
    on the response (e.g. ``status="list_models_failed"`` with
    ``description="invalid x-api-key"``). A 2xx therefore means "stored", not
    "works" — surface the difference rather than reporting a bad key as OK.
    """
    status = payload.get("status")
    if status and status not in _KEY_STATUS_OK:
        logger.warning(
            "Bifrost: provider %s key stored but reports status=%s (%s)",
            provider_name,
            status,
            payload.get("description") or "no detail",
        )


def _upsert_provider_key(
    provider_name: str,
    client: httpx.Client,
    *,
    key_value: str,
    models: Optional[List[str]] = None,
) -> bool:
    """Create or replace ``provider_name``'s key with ``key_value``.

    ``key_value`` must be the real secret from the secrets store, never a
    value read back from Bifrost (see the module docstring on masking).

    ``models`` replaces the allow-list; omit it to carry the existing one
    forward. There is no models-only update — every write carries the secret.
    """
    keys = _get_provider_keys(provider_name, client)
    if keys is None:
        return False

    existing = keys[0] if keys else None
    body: Dict[str, Any] = {
        k: v for k, v in (existing or {}).items() if k not in _KEY_READBACK_ONLY
    }
    body.setdefault("name", f"default-{provider_name}-key")
    body.setdefault("weight", 1)
    body.setdefault("enabled", True)
    # A bare string: wrapped as {"value": ..., "type": "plain_text"} this Bifrost
    # accepts the write and stores the serialized wrapper as the credential, after
    # which every call 401s. Verified against the running image rather than inferred.
    body["value"] = key_value
    if models is not None:
        body["models"] = models
    body.setdefault("models", [])

    verb = "PUT" if existing else "POST"
    url = (
        _keys_url(provider_name, existing["id"])
        if existing
        else _keys_url(provider_name)
    )
    try:
        r = (
            client.put(url, json=body, timeout=_DEFAULT_TIMEOUT)
            if existing
            else client.post(url, json=body, timeout=_DEFAULT_TIMEOUT)
        )
        if r.status_code >= 400:
            logger.warning(
                "Bifrost: %s %s returned %s: %s",
                verb,
                url,
                r.status_code,
                r.text[:200],
            )
            return False
        try:
            _log_key_health(provider_name, r.json())
        except Exception:  # noqa: BLE001 - health logging must never fail a write
            pass
        return True
    except Exception as e:
        logger.warning("Bifrost: %s %s failed: %s", verb, url, e)
        return False


def _delete_provider_keys(provider_name: str, client: httpx.Client) -> bool:
    """Remove ``provider_name``'s keys — the only way to clear a credential.

    Deleting is safe because ``_upsert_provider_key`` recreates the key from
    nothing when one is next configured.
    """
    keys = _get_provider_keys(provider_name, client)
    if keys is None:
        return False
    if not keys:
        return True
    ok = True
    for key in keys:
        try:
            r = client.delete(
                _keys_url(provider_name, key["id"]), timeout=_DEFAULT_TIMEOUT
            )
            if r.status_code >= 400:
                logger.warning(
                    "Bifrost: DELETE key %s on %s returned %s: %s",
                    key.get("id"),
                    provider_name,
                    r.status_code,
                    r.text[:200],
                )
                ok = False
        except Exception as e:
            logger.warning(
                "Bifrost: DELETE key %s on %s failed: %s",
                key.get("id"),
                provider_name,
                e,
            )
            ok = False
    return ok


def push_provider_key(provider_name: str, key_value: str) -> bool:
    """Set ``provider_name``'s Bifrost credential to ``key_value``.

    Creates the key if the provider has none, otherwise replaces the existing
    one in place. An empty ``key_value`` clears the credential by deleting the
    key. The existing model allow-list is carried forward untouched.

    Returns True on success. Any failure is logged and returns False so the
    caller's CRUD flow never breaks on a Bifrost hiccup — callers that surface
    the result to a user should report that False rather than swallow it.
    """
    if not provider_name:
        return False
    if provider_name in _UNMANAGED_PROVIDER_TYPES:
        logger.debug("Bifrost: provider %s manages its own key", provider_name)
        return True
    with httpx.Client() as client:
        if not key_value:
            return _delete_provider_keys(provider_name, client)
        ok = _upsert_provider_key(provider_name, client, key_value=key_value)
        if ok:
            logger.info("Bifrost: pushed updated key for provider %s", provider_name)
        return ok


def sync_all_provider_keys() -> Dict[str, bool]:
    """Push every DB-configured provider's current secret value to Bifrost.

    Run on backend startup so Bifrost picks up whatever is in the secrets
    store regardless of how it was started or whether its container was
    recreated. Best-effort — returns a per-provider dict of success flags.
    """
    # Deferred imports to keep this module import-cheap for code that only
    # needs ``push_provider_key`` (e.g. llm_providers.py).
    from core.secrets_manager import get_secret
    from core.storage.connection import get_db_manager
    from core.storage.models import LLMProviderConfig

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


def sync_provider_models(
    provider_type: str, model_ids: list[str], *, key_value: Optional[str]
) -> bool:
    """Update Bifrost's allow-list of routable models for ``provider_type``.

    Writes the allow-list through the provider's key, which is where Bifrost
    stores it, so every such write carries the credential. ``key_value`` is
    the caller's already-resolved secret for this provider type — it is passed
    in rather than looked up here so that discovery and this sync can never
    disagree about which secret backs a provider. A provider with no resolved
    secret has no allow-list to sync.

    Empty lists are skipped — wiping the allow-list to ``[]`` would cause
    Bifrost to reject every subsequent LLM call for that provider, which we
    never want just because an upstream API had a momentary hiccup.
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
    # Self-hosted Ollama serves whatever the user pulled/built, and its key
    # holds a URL rather than a secret we own. The seeded wildcard already
    # covers it, so there is nothing to write.
    if provider_type in _UNMANAGED_PROVIDER_TYPES:
        logger.debug(
            "Bifrost sync: provider %s uses a static allow-list", provider_type
        )
        return True

    seen: set = set()
    normalized: List[str] = []
    for mid in model_ids:
        if not mid or mid in seen:
            continue
        seen.add(mid)
        normalized.append(mid)

    if not key_value:
        logger.info(
            "Bifrost sync: no resolved secret for provider %s — "
            "skipping model allow-list sync",
            provider_type,
        )
        return False

    with httpx.Client() as client:
        ok = _upsert_provider_key(
            provider_type, client, key_value=key_value, models=normalized
        )
        if ok:
            logger.info(
                "Bifrost: synced %d models for provider %s",
                len(normalized),
                provider_type,
            )
        return ok


async def sync_all_provider_models() -> Dict[str, Any]:
    """Canonical refresh for every active LLM provider.

    Single source of truth — called at startup, on a schedule, from the
    refresh endpoints, and lazily on a dropdown cache miss. One call
    does everything:

    1. Fetches each provider's live upstream catalog via
       ``core.llm.providers.discovery``.
    2. Applies the configured extras (IDs upstream dropped from
       /v1/models but that still route — e.g. Claude 3.x).
    3. Populates ``_MODEL_LIST_CACHE[provider_id]`` in
       ``core.llm.providers.registry`` so the UI dropdown reads the same
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
    from core.llm.providers import discovery
    from core.llm.providers.registry import (
        _FALLBACK_MODELS_BY_PROVIDER,
        _MODEL_LIST_CACHE,
        _register_extras,
        get_extra_model_ids,
        record_live_meta,
    )
    from core.storage.connection import get_db_manager
    from core.storage.models import LLMProviderConfig

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
                    "is_default": bool(row.is_default),
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
        # Bifrost holds one key per provider *type* while Vigil can hold
        # several rows of that type, so the allow-list push below rides on a
        # single row's secret. Prefer the default row, matching the survivor
        # ``llm_providers._reconcile_bifrost_key_for_type`` would pick.
        provider_rows.sort(key=lambda r: not r["is_default"])
        type_key: Optional[str] = None

        for row_dict in provider_rows:
            row_ids: List[str] = []
            row_seen: set = set()
            upstream_ok = False
            row_key = _resolve_row_key(row_dict)
            if type_key is None:
                type_key = row_key

            try:
                meta = await _fetch_meta_for_row(row_dict, discovery, row_key)
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
            _MODEL_LIST_CACHE[row_dict["provider_id"]] = row_ids
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

        bifrost_results[provider_type] = sync_provider_models(
            provider_type, type_union, key_value=type_key
        )

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


def _resolve_row_key(row_dict: Dict[str, Any]) -> Optional[str]:
    """Resolve one provider row's plaintext secret, or None.

    The row's own ``api_key_ref`` wins; the env-held names are a fallback for
    deployments that never wrote a per-row ref. This is the single resolver
    for both catalog discovery and the Bifrost allow-list push, so the two
    cannot disagree about which secret backs a provider.
    """
    from core.secrets_manager import get_secret

    provider_type = row_dict["provider_type"]
    api_key_ref = row_dict.get("api_key_ref")

    if api_key_ref:
        try:
            val = get_secret(api_key_ref)
            if val:
                return val
        except Exception as exc:  # noqa: BLE001
            logger.debug("secret lookup for %s failed: %s", api_key_ref, exc)
    if provider_type == "anthropic":
        return get_secret("ANTHROPIC_API_KEY") or get_secret("CLAUDE_API_KEY")
    if provider_type == "openai":
        return get_secret("OPENAI_API_KEY")
    return None


async def _fetch_meta_for_row(
    row_dict: Dict[str, Any], discovery, key: Optional[str] = None
) -> Optional[list]:
    """Call the appropriate discovery function for one provider row.

    ``key`` is the row's resolved secret from ``_resolve_row_key``; it is
    passed in so the caller can reuse it for the Bifrost push instead of
    resolving twice.

    Returns ``None`` when the row isn't usable (e.g. no API key). The
    caller logs and skips.
    """
    provider_type = row_dict["provider_type"]
    base_url = row_dict.get("base_url")
    config = row_dict.get("config") or {}

    if provider_type == "anthropic":
        if not key:
            logger.info(
                "Bifrost sync: no Anthropic key available for %s — skipping",
                row_dict["provider_id"],
            )
            return None
        return await discovery.fetch_anthropic_models(key, base_url=base_url)

    if provider_type == "openai":
        # A key is required only for the real OpenAI cloud; a self-hosted
        # OpenAI-compatible server (vLLM, LM Studio) on a loopback/private
        # address is keyless. Pass the key through (may be None) and let
        # fetch_openai_models enforce it for the allowlisted cloud host only,
        # and allow_loopback so the SSRF IP gate doesn't reject an RFC1918
        # host — the same admin-gated trust anchor as the test and
        # discover-models endpoints, and mirroring the ollama branch below.
        # Without this, a self-hosted provider that discovers fine never syncs
        # into Bifrost. The caller wraps this in try/except and skips on error.
        return await discovery.fetch_openai_models(
            key,
            base_url=base_url,
            organization=config.get("organization"),
            allow_loopback=True,
        )

    if provider_type == "ollama":
        # ``base_url`` comes from a persisted provider row, which only a
        # ``settings.write`` admin can create/update (shape-validated at
        # save time). Self-hosted Ollama on a loopback/private address is
        # the expected deployment, so skip the SSRF IP gate here — the
        # same trust anchor as the admin-gated test and discover-models
        # endpoints. Without this, sync fails for any RFC1918 host even
        # though the UI dropdown populated fine.
        return await discovery.fetch_ollama_models(base_url, allow_loopback=True)

    logger.debug("Bifrost sync: unsupported provider_type %s", provider_type)
    return None


def sync_after_ollama_start() -> dict:
    """Push the freshly-reachable Ollama catalog into Bifrost's live config.

    Starting Ollama alone accomplishes nothing user-visible: LLM traffic is
    dispatched through Bifrost, and ``infra/docker/bifrost/config.json`` is only
    a first-boot seed (live config lives in Bifrost's SQLite). Without this the
    button "succeeds" and no Ollama model is selectable.

    Passed as the ``post_start_sync`` argument to
    ``core.platform.ollama_supervisor.start`` by a composition root — platform
    supervises the process, this decides what a running Ollama means for the
    model catalog. Awaits the sync rather than firing it off so the caller can
    report ``bifrost_synced`` truthfully. Callers run in a threadpool thread
    with no running loop; if a loop *is* running we fall back to scheduling,
    since ``asyncio.run`` would raise.
    Best-effort throughout — a Bifrost still booting must not fail the start.
    """
    import asyncio

    try:
        from core.llm.providers.registry import invalidate_model_cache

        invalidate_model_cache()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(sync_all_provider_models())
            return {"bifrost_synced": True}
        asyncio.get_running_loop().create_task(sync_all_provider_models())
        return {"bifrost_synced": False, "bifrost_sync_scheduled": True}
    except Exception as e:  # noqa: BLE001
        logger.info("Bifrost model sync after Ollama start did not complete: %s", e)
        return {"bifrost_synced": False, "bifrost_sync_error": str(e)}
