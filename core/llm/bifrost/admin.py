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

Four properties of that API shape the code below, all learned the hard way:

* **Keys are a subresource.** They live at ``/api/providers/{name}/keys``,
  *not* on the ``/api/providers/{name}`` document — which returns no ``keys``
  field at all.
* **Secrets are masked on read** (``sk-a****key``) and a write that echoes the
  mask is accepted with a 200, storing the mask as the credential. So values
  are always re-read from the secrets store, never round-tripped.
* **A key must carry a non-empty value.** There is no models-only update, and
  clearing a credential means deleting the key rather than blanking it.
* **Custom OpenAI-compatible ``base_url`` lives on the provider document.**
  Vigil stores the SDK form (often with a trailing ``/v1``). Bifrost stores
  the API root under ``network_config.base_url`` and appends the versioned
  path itself, so only that suffix is trimmed. This PUT must not send
  ``keys``. Anthropic ignores the field; Ollama's stored URL is host-side
  and would mean the Bifrost container itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from core.config import get_settings
from core.platform.url_safety import DEFAULT_ALLOWED_PROVIDER_HOSTS

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

# Public OpenAI cloud — Bifrost already points there. Custom OpenAI-compatible
# hosts (OpenRouter, LiteLLM, vLLM, ...) are the ones we must push. Derived
# from the SSRF allow-list so this file never names the stock host (one-egress
# ratchet); ``.openai.com`` keeps Azure-style hosts from matching.
_STOCK_OPENAI_HOSTS = frozenset(
    host for host in DEFAULT_ALLOWED_PROVIDER_HOSTS if host.endswith(".openai.com")
)

# Fields Bifrost accepts on PUT /api/providers/{name}. Everything else on the
# GET document is read-back (name, status, config_hash, keys, ...).
_PROVIDER_WRITE_FIELDS = frozenset(
    {
        "network_config",
        "concurrency_and_buffer_size",
        "proxy_config",
        "send_back_raw_request",
        "send_back_raw_response",
        "store_raw_request_response",
        "custom_provider_config",
    }
)

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


def _provider_url(provider_name: str) -> str:
    return f"{_bifrost_base_url()}/api/providers/{provider_name}"


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


def _normalize_openai_compat_base_url(base_url: str) -> str:
    """Strip a trailing ``/v1``; Bifrost appends the versioned path itself.

    Any other path prefix (``/api``, ``/openai``, a tenant slug) is kept.
    """
    url = base_url.strip().rstrip("/")
    if url.lower().endswith("/v1"):
        url = url[:-3].rstrip("/")
    return url


def _custom_openai_base_url(base_url: Optional[str]) -> Optional[str]:
    """Normalized Bifrost form, or None when the stock host should stay."""
    if not base_url or not str(base_url).strip():
        return None
    target = _normalize_openai_compat_base_url(str(base_url))
    if not target:
        return None
    host = (urlparse(target).hostname or "").lower()
    if host in _STOCK_OPENAI_HOSTS:
        return None
    return target


def _get_provider_document(name: str, client: httpx.Client) -> Optional[Dict[str, Any]]:
    """Return ``name``'s provider document, or None if it is absent."""
    try:
        r = client.get(_provider_url(name), timeout=_DEFAULT_TIMEOUT)
        if r.status_code == 404:
            logger.debug("Bifrost: provider %s not configured", name)
            return None
        r.raise_for_status()
        doc = r.json()
        return doc if isinstance(doc, dict) else None
    except Exception as e:
        logger.warning("Bifrost: could not fetch provider %s: %s", name, e)
        return None


def sync_provider_base_url(provider_type: str, base_url: Optional[str]) -> bool:
    """Write a custom OpenAI-compatible ``base_url`` to Bifrost.

    Only ``openai`` rows with a non-stock host are pushed. Anthropic ignores
    ``network_config.base_url`` (see ``core.llm.providers.clients``); Ollama's
    stored URL is host-side and would resolve to the Bifrost container itself.

    GET/PUT ``/api/providers/{name}`` (the provider document, not ``/keys``).
    Other ``network_config`` fields are preserved; only writable provider
    fields are sent, never ``keys``. Reverting to empty/stock clears a
    previously pushed custom host so inference does not keep using it.

    Returns True on success or when there is nothing to write. Failures are
    logged and return False so the catalog sync still proceeds.
    """
    if not provider_type or provider_type != "openai":
        return True
    target = _custom_openai_base_url(base_url)

    with httpx.Client() as client:
        doc = _get_provider_document(provider_type, client)
        if doc is None:
            # Stock/empty has nothing to write; a custom host cannot land if
            # the provider document is missing.
            return target is None
        network = dict(doc.get("network_config") or {})
        current = (network.get("base_url") or "").rstrip("/")
        if target is None:
            # Leave Bifrost's default host. If we previously pushed a custom
            # URL, drop it so inference does not keep hitting the old gateway.
            if not current or _custom_openai_base_url(current) is None:
                return True
            network.pop("base_url", None)
        elif current == target:
            return True
        else:
            network["base_url"] = target
        body = {k: v for k, v in doc.items() if k in _PROVIDER_WRITE_FIELDS}
        body["network_config"] = network
        url = _provider_url(provider_type)
        try:
            r = client.put(url, json=body, timeout=_DEFAULT_TIMEOUT)
            if r.status_code >= 400:
                logger.warning(
                    "Bifrost: PUT %s returned %s: %s",
                    url,
                    r.status_code,
                    r.text[:200],
                )
                return False
            logger.info(
                "Bifrost: set network_config.base_url for provider %s to %s",
                provider_type,
                target or "default",
            )
            return True
        except Exception as e:
            logger.warning("Bifrost: PUT %s failed: %s", url, e)
            return False


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
    4. For OpenAI-compatible rows, writes ``network_config.base_url`` so
       the gateway uses the custom host rather than the stock OpenAI cloud.
    5. Unions per-provider-type across rows and PUTs that to Bifrost's
       allow-list via the admin API, so LLM traffic routes for every
       model the dropdown shows.

    Because all four surfaces (dropdown cache, live-meta cache, Bifrost
    host, Bifrost allow-list) are written in the same pass, they cannot drift.

    Concurrent calls are coalesced — if a sync is already running (e.g.
    the scheduled refresher kicked off at the same time as a dropdown
    cold-load), the second caller awaits the same future rather than
    issuing a duplicate round of upstream fetches.

    Best-effort — never raises. Returns a dict with per-provider-type
    Bifrost sync flags under ``bifrost``, custom-host flags under
    ``bifrost_base_url``, and the computed per-row model lists under
    ``models_by_provider`` for observability.
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
        _LIVE_CATALOGUES,
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

    # A key configured in Bifrost alone has no row for the loop below to find.
    from core.llm.bifrost.mirror import reconcile_all

    await reconcile_all()

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
    base_url_results: Dict[str, bool] = {}
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

            # A stand-in when the provider has a fetcher that could not answer,
            # and the real thing when it has none.
            stood_in = False
            if meta is None and provider_type not in _HOST_OWNED_CATALOGUE:
                meta = await fetch_catalogue_models(provider_type)
                stood_in = meta is not None and provider_type in _DISCOVERABLE

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
            if not upstream_ok or stood_in:
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
            # A stood-in datasheet is not this provider's catalogue: ruling a
            # model out from it is the silent substitution ``can_serve`` exists
            # to avoid, and its list is missing whatever the bootstrap carries.
            if upstream_ok and not stood_in:
                _LIVE_CATALOGUES.add(row_dict["provider_id"])
            else:
                _LIVE_CATALOGUES.discard(row_dict["provider_id"])
            per_row_models[row_dict["provider_id"]] = row_ids

            # Contribute to the per-type union for Bifrost.
            for mid in row_ids:
                if mid in type_seen:
                    continue
                type_seen.add(mid)
                type_union.append(mid)

        # Host before allow-list: namespaced models are meaningless if the
        # gateway still talks to the stock OpenAI cloud. One document per type.
        if provider_type == "openai":
            # The first row that states a host, not simply the first row: a
            # mirror row carries no base_url and claims is_default when the
            # type has none, so sorting alone let it shadow a real row's custom
            # host and revert self-hosted traffic to the stock cloud.
            hosted = next(
                (r for r in provider_rows if r.get("base_url")), provider_rows[0]
            )
            base_url_results[provider_type] = sync_provider_base_url(
                provider_type, hosted.get("base_url")
            )

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
        "bifrost_base_url": base_url_results,
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
        return await _list_ollama_models(base_url, discovery)

    logger.debug("Bifrost sync: no fetcher for provider_type %s", provider_type)
    return None


# ---------------------------------------------------------------------------
# Gateway catalogue (providers with no upstream fetcher)
# ---------------------------------------------------------------------------

# Bifrost's ``/api/models/details`` datasheet: the only catalogue for a provider
# ``discovery.py`` cannot query, or holds no key for (every mirrored row).
#
# Reading it is not a second LLM path: it is capability discovery against the
# gateway we already route through, the same carve-out ``discovery.py``
# documents for querying provider catalogues directly.

# Bifrost's ollama catalogue is the generic library, not what a host pulled.
_HOST_OWNED_CATALOGUE = frozenset({"ollama"})

# Types ``_fetch_meta_for_row`` has a real fetcher for. For anything else the
# datasheet is the only catalogue there will ever be, so it can be trusted as
# one; for these it is a stand-in for a fetch that failed or had no key.
_DISCOVERABLE = frozenset({"anthropic", "openai", "ollama"})

# What the row's ``default_model`` should floor to, per provider type. That
# field is only a floor — it is what the picker shows when discovery yields
# nothing (#409) and what dispatch uses when a caller names no model; the
# operator's real choice is the ``chat_default`` assignment. So it needs to be
# *a* working chat model, not the right one, and preferring a mid-tier general
# model keeps the floor cheap rather than pinning the priciest id in a
# 136-model catalogue. First one present wins; absent all of them, the
# catalogue's first entry.
_CATALOG_DEFAULT_PREFERENCE: Dict[str, tuple] = {
    "vertex": ("gemini-2.5-flash", "gemini-2.0-flash", "claude-sonnet-4-5"),
    # Mid-tier, rather than the opus the bootstrap list leads with or the haiku
    # the marker sweep would otherwise land on.
    "anthropic": ("claude-sonnet-4-6",),
}


def _is_chat_catalogue_entry(entry: Dict[str, Any]) -> bool:
    """True when a datasheet entry is a chat model rather than another modality.

    Vertex serves imagen, veo, chirp, OCR and embeddings out of the same
    catalogue. A chat model is one that can emit tokens, so the presence of
    ``max_output_tokens`` is the discriminator; embedding ids carry an input
    limit only and are also caught by name, since their families are the one
    set ``discovery.py`` already knows how to spot.
    """
    from core.llm.providers.discovery import is_embedding_model_id

    name = entry.get("name") or ""
    if not name or is_embedding_model_id(name):
        return False
    return bool(entry.get("max_output_tokens"))


async def fetch_catalogue_models(provider_type: str) -> Optional[List[Any]]:
    """Return Bifrost's own catalogue for ``provider_type`` as ``ModelMeta``.

    Returns None when the catalogue is unreachable or holds no chat models, so
    the caller falls back to its bootstrap list rather than caching a blank.
    """
    from core.llm.providers.discovery import ModelMeta

    url = f"{_bifrost_base_url()}/api/models/details"
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.get(
                url, params={"provider": provider_type, "limit": 1000}
            )
            resp.raise_for_status()
            entries = resp.json().get("models") or []
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("Bifrost catalogue fetch failed for %s: %s", provider_type, exc)
        return None

    meta = [
        ModelMeta(
            id=e["name"],
            display_name=e["name"],
            context_window=int(e.get("max_input_tokens") or 0),
            # The list endpoint carries no capability flags (they live on the
            # per-model ``/parameters`` route, one call each). Tool use is the
            # one Vigil depends on and every chat family Vertex serves —
            # Claude, Gemini, Mistral, Llama — supports it, so default it True
            # for the same reason ``_anthropic_caps`` does. Thinking and vision
            # are left unset rather than guessed, since nothing depends on them.
            capabilities={"supports_tools": True},
        )
        for e in entries
        if _is_chat_catalogue_entry(e)
    ]
    if not meta:
        logger.warning(
            "Bifrost catalogue for %s held no chat models (%d entries)",
            provider_type,
            len(entries),
        )
        return None
    return meta


async def _list_ollama_models(
    base_url: Optional[str], discovery: Any = None
) -> Optional[List[Any]]:
    """List an Ollama server's pulled models, or None if none can be reached.

    A mirrored row carries no ``base_url`` of its own, and ``fetch_ollama_models``
    then defaults to loopback — right only for a single-machine install, and
    this process is as likely to be containerised as the gateway is. So a row
    without one falls to ``OLLAMA_URL`` before that default. The catalogue and
    the row's ``default_model`` both resolve through here; reading them from
    different endpoints gave a row a servable default and an empty picker.

    A server that answers with nothing pulled has answered: only a failure to
    reach one moves on to the next candidate.
    """
    if discovery is None:
        from core.llm.providers import discovery as discovery_module

        discovery = discovery_module

    candidates: List[Optional[str]] = [base_url] if base_url else []
    configured = (get_settings().ollama_url or "").strip()
    for candidate in (configured, None):
        if candidate and candidate in candidates:
            continue
        candidates.append(candidate)

    for candidate in candidates:
        try:
            return await discovery.fetch_ollama_models(candidate, allow_loopback=True)
        except Exception as exc:  # noqa: BLE001 - try the next endpoint
            logger.debug("Could not list Ollama models at %s: %s", candidate, exc)
    return None


async def _first_pulled_ollama_model() -> Optional[str]:
    """The first model this host has actually pulled, or None if it can't say."""
    models = await _list_ollama_models(None)
    return models[0].id if models else None


async def default_model_for_provider_type(provider_type: str) -> Optional[str]:
    """Pick the ``default_model`` a mirrored provider row should floor to.

    None when no model can be named, and the caller then skips the row: a
    default is what a bad model falls back *to*, so nothing downstream can
    correct one. A missing row self-heals on the next sync; a wrong one does
    not.
    """
    from core.llm.providers.registry import _FALLBACK_MODELS_BY_PROVIDER

    if provider_type in _HOST_OWNED_CATALOGUE:
        pulled = await _first_pulled_ollama_model()
        if not pulled:
            logger.warning(
                "No pulled model to floor a mirrored %s row to — leaving it "
                "unmirrored until the server can be listed",
                provider_type,
            )
        return pulled

    bootstrap = _FALLBACK_MODELS_BY_PROVIDER.get(provider_type) or ()
    if bootstrap:
        return _preferred_floor(provider_type, list(bootstrap))

    meta = await fetch_catalogue_models(provider_type)
    if meta:
        return _preferred_floor(provider_type, [m.id for m in meta])

    logger.warning(
        "No catalogue or bootstrap model for provider type %s — leaving it "
        "unmirrored",
        provider_type,
    )
    return None


_MID_TIER_MARKERS = ("flash", "mini", "haiku", "lite", "small", "nano")


def _preferred_floor(provider_type: str, ids: List[str]) -> str:
    """Which of ``ids`` a mirrored row should floor its ``default_model`` to.

    One path for both sources. The bootstrap list used to be taken at face
    value, so anthropic floored to the priciest id it declares while the
    catalogue path was busy avoiding exactly that.
    """
    for preferred in _CATALOG_DEFAULT_PREFERENCE.get(provider_type, ()):
        if preferred in ids:
            return preferred
    for marker in _MID_TIER_MARKERS:
        for candidate in ids:
            if marker in candidate.lower():
                return candidate
    return ids[0]


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
