"""Mirror Bifrost's providers into ``llm_provider_configs``.

Bifrost's config store holds the credentials and does the routing, but the rest
of Vigil indexes providers by ``llm_provider_configs`` row: the model picker
aggregates over those rows (``ModelRegistry.list_available_models``),
``ai_model_configs.provider_id`` FKs to one, and ``get_provider_spec`` resolves
dispatch through one. Two-store readiness (#761) taught the setup gate to read
Bifrost directly, but only the gate — so a key added in Bifrost alone put a
checkmark on the first setup step and left every surface past it reading a
table nothing had written: an empty picker, no assignment savable, no spec to
dispatch through.

A mirror row closes that. It is an index entry, not a second config: no
``api_key_ref`` (Bifrost holds the secret) and no ``base_url`` (the gateway
owns routing). One row per provider *type*, which is all the dispatch path
reads off it — Bifrost load-balances the keys within a provider itself.

Rows are written from two places, both of which converge on the same state:
the config proxy on an accepted key write (``services/api/routers/bifrost_config``),
and ``reconcile_all`` on every catalogue sync, which backfills providers that
were configured before this existed.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0

_MIRROR_PREFIX = "bifrost-"

# Bifrost reports two different things as ``list_models_failed``: a credential
# it rejected ("API key is invalid.") and one it could not check at all. Nothing
# in the status separates them, so both discriminators below are allowlists.
#
# Vertex cannot be list-verified under any auth Vigil offers: under an API key
# Bifrost refuses before trying, and under a service account or ADC the listing
# demands a quota project the gateway does not send while ``generateContent`` on
# the same credential works. The check is broken, not the credential — the same
# conclusion ``_UNMANAGED_PROVIDER_TYPES`` in admin.py reaches about vertex's
# catalogue. A provider that CAN be listed and failed stays a real failure.
#
# This module is the ONLY implementation. The console and scripts/reset-setup.sh
# used to restate it in TypeScript and in bash-embedded Python; all three drifted
# the moment vertex's second failure mode turned up, so both now read the verdict
# from ``GET /api/bifrost/routability`` (services/api/routers/bifrost_config.py).
_UNVERIFIABLE_PROVIDERS = frozenset({"vertex"})
_LIST_UNSUPPORTED = re.compile(r"not supported for list models", re.I)


def _list_check_unavailable(provider: Optional[str], description: str) -> bool:
    if provider and provider in _UNVERIFIABLE_PROVIDERS:
        return True
    return bool(_LIST_UNSUPPORTED.search(description))


def _cred_from_env(key: Dict[str, Any]) -> bool:
    """True when the credential points at an env var rather than being set.

    A first-boot seed's keys reference ``env.ANTHROPIC_API_KEY`` and friends,
    which on a fresh install are unset. Mirroring those would offer providers
    that cannot route and would green the setup step for a install nobody has
    configured — exactly what #761 exists to prevent.
    """
    for candidate in (
        key.get("value"),
        (key.get("vertex_key_config") or {}).get("auth_credentials"),
    ):
        if isinstance(candidate, dict) and candidate.get("from_env"):
            return True
    return False


def key_is_routable(key: Dict[str, Any], provider: Optional[str] = None) -> bool:
    """True when a Bifrost key can actually route.

    ``success`` means Bifrost verified the credential upstream. Absent or
    ``unknown`` means it has not checked; an unlistable auth type means it never
    will. Both of those are taken on trust — there is no signal to be had — so a
    wrong credential fails on first use rather than here.
    """
    if not key.get("enabled", True):
        return False
    status = key.get("status")
    if status == "success":
        return True
    if not status or status == "unknown":
        return not _cred_from_env(key)
    if status == "list_models_failed" and _list_check_unavailable(
        provider, key.get("description") or ""
    ):
        return not _cred_from_env(key)
    return False


def _mirror_id(provider: str) -> str:
    return f"{_MIRROR_PREFIX}{provider}"


def _session_scope():
    from core.storage.connection import get_db_manager

    db = get_db_manager()
    if db._engine is None:
        db.initialize()
    return db.session_scope()


def _upsert_row(
    provider: str, default_model: str, servable: Optional[set] = None
) -> None:
    from core.storage.models import LLMProviderConfig

    with _session_scope() as session:
        row = session.get(LLMProviderConfig, _mirror_id(provider))
        if row is not None:
            row.is_active = True
            # Otherwise left alone: an operator may have retitled the row or
            # chosen its default_model, and re-adding a key must not stamp over
            # that. But a default the provider no longer serves is not a choice
            # -- an `ollama rm` of that model 404s every call that names none.
            if servable and row.default_model not in servable:
                logger.info(
                    "Mirror row %s defaulted to %s, which %s no longer serves — "
                    "moving it to %s",
                    _mirror_id(provider),
                    row.default_model,
                    provider,
                    default_model,
                )
                row.default_model = default_model
            return
        # ``llm_provider_default_per_type`` is unique on provider_type where
        # is_default, so claim the default only when the type has none.
        has_default = (
            session.query(LLMProviderConfig)
            .filter(
                LLMProviderConfig.provider_type == provider,
                LLMProviderConfig.is_default.is_(True),
            )
            .first()
            is not None
        )
        session.add(
            LLMProviderConfig(
                provider_id=_mirror_id(provider),
                provider_type=provider,
                name=f"{provider} (via Bifrost)",
                default_model=default_model,
                is_active=True,
                is_default=not has_default,
                config={"managed_by": "bifrost"},
            )
        )
        logger.info(
            "Mirrored Bifrost provider %s as %s (default_model=%s)",
            provider,
            _mirror_id(provider),
            default_model,
        )


def _deactivate_row(provider: str) -> None:
    """Retire the mirror once its provider has no routable key left.

    Deactivated, not deleted: ``ai_model_configs.provider_id`` is ON DELETE
    RESTRICT, so a row a component is still assigned to cannot be removed at
    all. ``is_active = False`` drops it from the picker and from the catalogue
    sync, which is the honest state either way.
    """
    from core.storage.models import LLMProviderConfig

    with _session_scope() as session:
        row = session.get(LLMProviderConfig, _mirror_id(provider))
        if row is not None and row.is_active:
            row.is_active = False
            logger.info("Retired mirror row %s — no routable key", _mirror_id(provider))


async def _get(path: str) -> Any:
    url = f"{get_settings().bifrost_url.rstrip('/')}/api/{path}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


async def _routable_keys(provider: str) -> List[Dict[str, Any]]:
    keys = (await _get(f"providers/{provider}/keys")).get("keys") or []
    return [k for k in keys if key_is_routable(k, provider)]


async def _apply(provider: str, *, routable: bool) -> None:
    if not routable:
        await asyncio.to_thread(_deactivate_row, provider)
        return
    from core.llm.bifrost.admin import default_model_for_provider_type

    default_model = await default_model_for_provider_type(provider)
    if not default_model:
        logger.warning(
            "Not mirroring provider %s — no servable default_model to floor it to",
            provider,
        )
        return
    servable = await _servable_models(provider)
    await asyncio.to_thread(_upsert_row, provider, default_model, servable)


async def _servable_models(provider: str) -> Optional[set]:
    """What ``provider`` serves right now, or None when that cannot be known.

    Only asked of a self-hosted server, whose set changes under the operator's
    hands. A cloud provider's catalogue does not shrink out from under a stored
    default, so there is nothing to re-heal and no request worth making.
    """
    from core.llm.bifrost.admin import _HOST_OWNED_CATALOGUE, _list_ollama_models

    if provider not in _HOST_OWNED_CATALOGUE:
        return None
    models = await _list_ollama_models(None)
    return {m.id for m in models} if models else None


async def sync_provider(provider: str) -> None:
    """Bring one provider's mirror row in step with its Bifrost keys.

    Best-effort: a key write Bifrost has accepted must not be undone by
    bookkeeping. It does need a log line, because the symptom downstream is an
    empty model picker with nothing else to go on.
    """
    try:
        await _apply(provider, routable=bool(await _routable_keys(provider)))
    except Exception as exc:  # noqa: BLE001 - never fail an accepted write
        logger.warning("Could not mirror provider row for %s: %s", provider, exc)


async def reconcile_all() -> Optional[Dict[str, bool]]:
    """Mirror every Bifrost provider, backfilling ones configured earlier.

    Returns the per-provider routable verdict, or None when Bifrost could not
    be reached. Best-effort throughout — this rides on the catalogue sync and
    must never fail it.
    """
    try:
        providers = (await _get("providers")).get("providers") or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("Mirror reconcile skipped — Bifrost unreachable: %s", exc)
        return None

    verdicts: Dict[str, bool] = {}
    for p in providers:
        name = p.get("name")
        if not name:
            continue
        try:
            routable = bool(await _routable_keys(name))
            await _apply(name, routable=routable)
            verdicts[name] = routable
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mirror reconcile failed for %s: %s", name, exc)
    return verdicts


# Health labels the console renders beside a key. Derived here rather than in the
# browser so the "could not check" / "was refused" split — the distinction that
# needed fixing twice in one afternoon — has a single implementation.
HEALTH_HEALTHY = "healthy"
HEALTH_UNVERIFIED = "unverified"
HEALTH_UNVERIFIABLE = "unverifiable"
HEALTH_REJECTED = "rejected"


def key_health(key: Dict[str, Any], provider: Optional[str] = None) -> str:
    """One of the ``HEALTH_*`` labels for a key's Bifrost status."""
    status = key.get("status")
    if status == "success":
        return HEALTH_HEALTHY
    if not status or status == "unknown":
        return HEALTH_UNVERIFIED
    if status == "list_models_failed" and _list_check_unavailable(
        provider, key.get("description") or ""
    ):
        return HEALTH_UNVERIFIABLE
    return HEALTH_REJECTED


async def routability() -> Dict[str, Any]:
    """Every consumer's answer to "can this key route?", computed once.

    Shaped for a single round trip: the console needs a per-key verdict to badge
    the settings table, the setup gate needs only "is any provider routable",
    and the reset script needs both. A provider whose keys cannot be listed is
    reported as not routable rather than omitted, so a caller never has to tell
    "no keys" apart from "could not ask".
    """
    providers = (await _get("providers")).get("providers") or []
    key_verdicts: Dict[str, Any] = {}
    provider_verdicts: Dict[str, bool] = {}

    for p in providers:
        name = p.get("name")
        if not name:
            continue
        try:
            keys = (await _get(f"providers/{name}/keys")).get("keys") or []
        except Exception as exc:  # noqa: BLE001 - one provider must not fail all
            logger.warning("Routability: could not list keys for %s: %s", name, exc)
            provider_verdicts[name] = False
            continue
        routable_here = False
        for k in keys:
            key_id = k.get("id")
            verdict = key_is_routable(k, name)
            routable_here = routable_here or verdict
            if key_id:
                key_verdicts[key_id] = {
                    "provider": name,
                    "routable": verdict,
                    "health": key_health(k, name),
                    "description": k.get("description") or None,
                }
        provider_verdicts[name] = routable_here

    return {"providers": provider_verdicts, "keys": key_verdicts}
