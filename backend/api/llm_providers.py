"""LLM provider management API — CRUD + test + model listing.

See GH issue #88. Each row in `llm_provider_configs` represents a configured
LLM backend (Anthropic, OpenAI, Ollama, ...). API keys are stored in the
secrets_manager under a generated ref name, never in the DB.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete, update
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).parent.parent))
from secrets_manager import delete_secret, get_secret, set_secret  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from backend.middleware.auth import get_current_active_user  # noqa: E402
from backend.services.auth_service import AuthService  # noqa: E402
from database.connection import get_db  # noqa: E402
from database.models import AIModelConfig, LLMProviderConfig, User  # noqa: E402
from services.bifrost_admin import push_provider_key  # noqa: E402
from services.url_safety import (  # noqa: E402
    UrlSafetyError,
    validate_provider_url,
)

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_PROVIDER_TYPES = {"anthropic", "openai", "ollama"}
_SLUG_RE = re.compile(r"[^a-z0-9-]+")

# Anthropic's live /v1/models endpoint is consulted via
# ``services.provider_model_discovery``; the fallback tuple here is the
# cold-boot list used only when the live call fails (e.g. no API key
# was provided at /discover-models time).
from services.model_registry import _FALLBACK_MODELS_BY_PROVIDER  # noqa: E402

ANTHROPIC_FALLBACK_MODELS = list(_FALLBACK_MODELS_BY_PROVIDER["anthropic"])


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-") or "provider"


def _secret_ref_for(provider_id: str) -> str:
    return f"llm_provider_{provider_id}_api_key"


def _require_settings_admin(current_user: User) -> None:
    """Raise 403 unless the user has ``settings.write``.

    Provider CRUD touches secrets and pushes them to Bifrost; the discover
    and test endpoints make outbound HTTP requests that can be steered
    by ``base_url``. Both gates are admin-only.
    """
    if not AuthService.check_permission(current_user.user_id, "settings.write"):
        raise HTTPException(
            status_code=403,
            detail="Permission denied: settings.write required",
        )


def _validate_provider_base_url_shape(base_url: Optional[str]) -> None:
    """Cheap shape-only validation for stored ``base_url``.

    Admins legitimately persist loopback URLs for self-hosted Ollama or
    private LLM gateways, so the SSRF IP gate doesn't apply here — it
    runs at HTTP-request time inside the discovery/test helpers. We
    still reject malformed inputs and obvious smuggling vectors (non-
    http(s) scheme, userinfo, fragment) so they never reach disk.
    """
    if base_url is None or not base_url.strip():
        return
    from urllib.parse import urlparse

    parsed = urlparse(base_url.strip())
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"base_url: scheme not allowed: {parsed.scheme or '(missing)'}",
        )
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="base_url: missing host")
    if parsed.username or parsed.password:
        raise HTTPException(
            status_code=400, detail="base_url: must not include userinfo"
        )
    if parsed.fragment:
        raise HTTPException(
            status_code=400, detail="base_url: must not include a fragment"
        )


class LLMProviderCreate(BaseModel):
    provider_id: Optional[str] = Field(
        None, description="If omitted, derived from name"
    )
    provider_type: str
    name: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    default_model: str
    is_active: bool = True
    is_default: bool = False
    config: Dict[str, Any] = Field(default_factory=dict)


class LLMProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    default_model: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None


class LLMProviderResponse(BaseModel):
    provider_id: str
    provider_type: str
    name: str
    base_url: Optional[str]
    has_api_key: bool
    default_model: str
    is_active: bool
    is_default: bool
    config: Dict[str, Any]
    last_test_at: Optional[str]
    last_test_success: Optional[bool]
    last_error: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]


def _to_response(row: LLMProviderConfig) -> Dict[str, Any]:
    d = row.to_dict()
    d.pop("api_key_ref", None)
    return d


def _validate_type(provider_type: str) -> None:
    if provider_type not in VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"provider_type must be one of {sorted(VALID_PROVIDER_TYPES)}",
        )


def _clear_other_defaults(db: Session, provider_type: str, keep_id: str) -> None:
    """Enforce the 'one default per provider_type' invariant at the app layer.

    The DB has a partial unique index (``llm_provider_default_per_type``,
    ``WHERE is_default = TRUE``) too, but clearing first avoids the
    index-conflict round-trip on UPDATE.

    The ``no_autoflush`` guard is load-bearing: callers set ``keep_id``'s
    ``is_default = True`` (or stage an INSERT with it) *before* calling here,
    so the Core UPDATE below would otherwise trigger an autoflush that writes
    the new default while the old one is still TRUE — two defaults at once,
    which the partial unique index rejects with an IntegrityError (500).
    Suppressing autoflush lets the UPDATE clear the old default first; the
    pending ``keep_id`` change then flushes safely at commit.
    """
    with db.no_autoflush:
        db.execute(
            update(LLMProviderConfig)
            .where(
                LLMProviderConfig.provider_type == provider_type,
                LLMProviderConfig.provider_id != keep_id,
                LLMProviderConfig.is_default.is_(True),
            )
            .values(is_default=False)
        )


def _reconcile_bifrost_key_for_type(
    db: Session, provider_type: str, exclude_provider_id: str
) -> None:
    """Reconcile Bifrost's shared per-type key after a provider loses its key.

    Bifrost stores a single key per ``provider_type`` (see
    ``bifrost_admin.push_provider_key`` — keyed by type, not by row), so
    blindly blanking that key whenever one provider of the type is deleted
    or cleared would knock out every same-type sibling that still relies on
    it (``_schedule_catalog_resync`` only re-syncs the model allow-list, not
    the keys, so the type would stay keyless until a restart).

    So: only blank Bifrost's key when this was the last provider of its type
    with a key; otherwise re-push a surviving sibling's key.
    ``exclude_provider_id`` is the row being deleted/cleared, so it never
    counts as a survivor.
    """
    survivors = [
        r
        for r in db.query(LLMProviderConfig).all()
        if r.provider_type == provider_type
        and r.provider_id != exclude_provider_id
        and r.api_key_ref
    ]
    # Prefer the default provider, then any active one, for a stable choice.
    survivors.sort(key=lambda r: (not r.is_default, not r.is_active))
    for survivor in survivors:
        value = get_secret(survivor.api_key_ref)
        if value:
            push_provider_key(provider_type, value)
            return
    # No surviving provider of this type has a usable key — it was the last
    # one, so clear the stale credential on Bifrost.
    push_provider_key(provider_type, "")


def _schedule_catalog_resync(reason: str) -> None:
    """Invalidate the model-list cache and fire a background sync.

    Called from provider CRUD so the UI dropdown and Bifrost's allow-list
    reflect the change immediately, rather than waiting up to the next
    scheduled refresh. Best-effort — never blocks the response.
    """
    import asyncio

    from services.bifrost_admin import sync_all_provider_models
    from services.model_registry import invalidate_model_cache

    invalidate_model_cache()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(sync_all_provider_models())
    logger.info("Model catalog resync scheduled: %s", reason)


@router.get("", response_model=List[LLMProviderResponse])
@router.get("/", response_model=List[LLMProviderResponse])
async def list_providers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    rows = db.query(LLMProviderConfig).order_by(LLMProviderConfig.created_at).all()
    return [_to_response(r) for r in rows]


@router.post("", response_model=LLMProviderResponse, status_code=201)
@router.post("/", response_model=LLMProviderResponse, status_code=201)
async def create_provider(
    payload: LLMProviderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _require_settings_admin(current_user)
    _validate_type(payload.provider_type)
    _validate_provider_base_url_shape(payload.base_url)

    provider_id = payload.provider_id or _slugify(payload.name)
    if db.get(LLMProviderConfig, provider_id) is not None:
        raise HTTPException(
            status_code=409, detail=f"provider_id '{provider_id}' already exists"
        )

    api_key_ref: Optional[str] = None
    if payload.api_key:
        api_key_ref = _secret_ref_for(provider_id)
        if not set_secret(api_key_ref, payload.api_key):
            raise HTTPException(status_code=500, detail="Failed to persist API key")
        # Push live to Bifrost so subsequent LLM calls use the new key
        # without waiting for a container restart.
        push_provider_key(payload.provider_type, payload.api_key)

    row = LLMProviderConfig(
        provider_id=provider_id,
        provider_type=payload.provider_type,
        name=payload.name,
        base_url=payload.base_url,
        api_key_ref=api_key_ref,
        default_model=payload.default_model,
        is_active=payload.is_active,
        is_default=payload.is_default,
        config=payload.config or {},
    )
    db.add(row)

    if payload.is_default:
        _clear_other_defaults(db, payload.provider_type, provider_id)

    db.commit()
    db.refresh(row)
    _schedule_catalog_resync(f"created provider {provider_id}")
    return _to_response(row)


@router.put("/{provider_id}", response_model=LLMProviderResponse)
async def update_provider(
    provider_id: str,
    payload: LLMProviderUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _require_settings_admin(current_user)
    _validate_provider_base_url_shape(payload.base_url)
    row = db.get(LLMProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    if payload.name is not None:
        row.name = payload.name
    if payload.base_url is not None:
        row.base_url = payload.base_url
    if payload.default_model is not None:
        row.default_model = payload.default_model
    if payload.is_active is not None:
        row.is_active = payload.is_active
    if payload.config is not None:
        row.config = payload.config

    if payload.api_key is not None:
        # Empty string clears the key; non-empty rotates it.
        ref = row.api_key_ref or _secret_ref_for(provider_id)
        if payload.api_key == "":
            delete_secret(ref)
            row.api_key_ref = None
            # Bifrost's key is shared per provider_type, so only blank it if
            # no same-type sibling still has a key — otherwise re-push the
            # survivor's so the type keeps a working credential.
            _reconcile_bifrost_key_for_type(db, row.provider_type, provider_id)
        else:
            if not set_secret(ref, payload.api_key):
                raise HTTPException(status_code=500, detail="Failed to persist API key")
            row.api_key_ref = ref
            push_provider_key(row.provider_type, payload.api_key)

    if payload.is_default is True:
        row.is_default = True
        _clear_other_defaults(db, row.provider_type, provider_id)
    elif payload.is_default is False:
        if row.is_default:
            # Guard: only enforce when transitioning True → False.
            other_default = (
                db.query(LLMProviderConfig)
                .filter(
                    LLMProviderConfig.provider_type == row.provider_type,
                    LLMProviderConfig.provider_id != provider_id,
                    LLMProviderConfig.is_default.is_(True),
                )
                .first()
            )
            if other_default is None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Cannot unset the only default provider. "
                        "Set another provider as default first."
                    ),
                )
        row.is_default = False

    db.commit()
    db.refresh(row)
    _schedule_catalog_resync(f"updated provider {provider_id}")
    return _to_response(row)


@router.delete("/{provider_id}")
async def delete_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _require_settings_admin(current_user)
    row = db.get(LLMProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    # Guard: refuse to delete the only active default for this provider type.
    if row.is_default:
        other_active = (
            db.query(LLMProviderConfig)
            .filter(
                LLMProviderConfig.provider_type == row.provider_type,
                LLMProviderConfig.provider_id != provider_id,
                LLMProviderConfig.is_active.is_(True),
            )
            .order_by(LLMProviderConfig.created_at)
            .first()
        )
        if other_active is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot delete the only active {row.provider_type} provider. "
                    "Add or activate another provider of this type first."
                ),
            )
        # Unset current default first, flush to satisfy the unique partial index,
        # then promote the next active provider.
        row.is_default = False
        db.flush()
        other_active.is_default = True

    # Cascade: remove model-config rows that reference this provider before
    # deleting — the FK is ON DELETE RESTRICT so this must happen first.
    db.execute(sa_delete(AIModelConfig).where(AIModelConfig.provider_id == provider_id))

    if row.api_key_ref:
        try:
            delete_secret(row.api_key_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to delete secret %s: %s", row.api_key_ref, exc)
        # Bifrost's key is shared per provider_type. Only wipe it if this was
        # the last keyed provider of its type; otherwise re-push a surviving
        # sibling's key so the type isn't left without a working credential.
        # (``row`` is still in the session here — exclude it explicitly.)
        _reconcile_bifrost_key_for_type(db, row.provider_type, provider_id)

    db.delete(row)
    db.commit()
    _schedule_catalog_resync(f"deleted provider {provider_id}")
    return {"success": True, "provider_id": provider_id}


@router.post("/{provider_id}/set-default", response_model=LLMProviderResponse)
async def set_default_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _require_settings_admin(current_user)
    row = db.get(LLMProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")
    row.is_default = True
    _clear_other_defaults(db, row.provider_type, provider_id)
    db.commit()
    db.refresh(row)
    return _to_response(row)


async def _resolve_api_key(row: LLMProviderConfig) -> Optional[str]:
    if not row.api_key_ref:
        return None
    return get_secret(row.api_key_ref)


async def _probe_provider_connection(
    *,
    provider_type: str,
    base_url: Optional[str],
    api_key: Optional[str],
    default_model: Optional[str] = None,
    organization: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Run the per-type connection probe against raw params (no DB row).

    Returns ``(success, error)``. Shared by the persisted ``/{id}/test``
    endpoint (which resolves the key from the secret store first) and the
    stateless ``/test-connection`` endpoint (which takes the key in the body,
    so a wizard can verify a provider before any row is created).
    """
    success = False
    error: Optional[str] = None

    try:
        if provider_type == "ollama":
            raw_base = base_url or "http://localhost:11434"
            # Ollama is the legitimate self-hosted provider, so an admin
            # who has saved a loopback URL is allowed to test it. We
            # still parse and sanitize to drop query string / userinfo
            # and reject non-http(s) schemes.
            from urllib.parse import urlparse, urlunparse

            parsed = urlparse(raw_base.strip())
            if parsed.scheme not in ("http", "https"):
                raise RuntimeError(f"scheme not allowed: {parsed.scheme}")
            if parsed.username or parsed.password or parsed.fragment:
                raise RuntimeError(
                    "ollama base_url must not include userinfo or fragment"
                )
            sanitized = urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc.split("@")[-1],
                    parsed.path or "",
                    "",
                    "",
                    "",
                )
            )
            async with httpx.AsyncClient(
                timeout=10.0, follow_redirects=False
            ) as client:
                resp = await client.get(f"{sanitized.rstrip('/')}/api/tags")
                resp.raise_for_status()
                success = True
        elif provider_type == "openai":
            try:
                safe = validate_provider_url(
                    base_url or "https://api.openai.com/v1",
                    allow_custom=True,
                )
            except UrlSafetyError as exc:
                raise RuntimeError(f"invalid base_url: {exc}") from exc
            if not api_key:
                raise RuntimeError("no api key configured")
            headers: Dict[str, str] = {}
            if safe.is_allowlisted_host:
                headers["Authorization"] = f"Bearer {api_key}"
                if organization:
                    headers["OpenAI-Organization"] = organization
            async with httpx.AsyncClient(
                timeout=15.0, follow_redirects=False
            ) as client:
                resp = await client.get(
                    f"{safe.sanitized.rstrip('/')}/models", headers=headers
                )
                resp.raise_for_status()
                success = True
        elif provider_type == "anthropic":
            if base_url:
                try:
                    validate_provider_url(base_url, allow_custom=True)
                except UrlSafetyError as exc:
                    raise RuntimeError(f"invalid base_url: {exc}") from exc
            if not api_key:
                raise RuntimeError("no api key configured")
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise RuntimeError("anthropic SDK not installed") from exc
            # Must use AsyncAnthropic (not Anthropic) — this endpoint is
            # async and the sync client would block the FastAPI event loop
            # for up to the full timeout on an invalid key or slow network.
            anthropic_kwargs: Dict[str, Any] = {"api_key": api_key, "timeout": 15.0}
            if base_url:
                anthropic_kwargs["base_url"] = base_url
            client = AsyncAnthropic(**anthropic_kwargs)
            await client.messages.create(
                model=default_model or "",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            success = True
        else:
            raise RuntimeError(f"unsupported provider_type: {provider_type}")
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        logger.info("Provider connection probe failed (%s): %s", provider_type, error)

    return success, error


@router.post("/{provider_id}/test")
async def test_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    _require_settings_admin(current_user)
    row = db.get(LLMProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    from datetime import datetime

    success, error = await _probe_provider_connection(
        provider_type=row.provider_type,
        base_url=row.base_url,
        api_key=await _resolve_api_key(row),
        default_model=row.default_model,
        organization=(row.config or {}).get("organization"),
    )

    row.last_test_at = datetime.utcnow()
    row.last_test_success = success
    row.last_error = None if success else error
    db.commit()

    return {"success": success, "provider_id": provider_id, "error": error}


class DiscoverModelsRequest(BaseModel):
    """Ephemeral model-discovery: takes unsaved credentials and returns the
    provider's model list. Used by the Add LLM Provider dialog to populate a
    dropdown before the provider is saved."""

    provider_type: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    organization: Optional[str] = None


class TestConnectionRequest(BaseModel):
    """Stateless connection test: takes unsaved credentials and probes the
    provider without persisting anything. Lets the Add LLM Provider wizard
    verify a connection before any row exists, so a cancelled wizard never
    strands a half-configured provider (and its claimed ``provider_id``)."""

    provider_type: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    default_model: Optional[str] = None  # anthropic's ping needs a model id
    organization: Optional[str] = None


@router.post("/discover-models")
async def discover_models(
    req: DiscoverModelsRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Pre-save model discovery for the Add Provider dialog.

    Admin-only because it makes an outbound HTTP request whose target
    is influenced by the request body (``base_url``). The URL is run
    through :func:`services.url_safety.validate_provider_url` inside
    each discovery helper, but we also require the caller to be an
    authenticated admin so a stolen session is the only path to even
    reach that validation.
    """
    _require_settings_admin(current_user)

    if req.provider_type not in VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported provider_type: {req.provider_type}",
        )

    from services import provider_model_discovery as discovery

    try:
        if req.provider_type == "anthropic":
            if not req.api_key:
                return {"models": ANTHROPIC_FALLBACK_MODELS}
            meta = await discovery.fetch_anthropic_models(
                req.api_key, base_url=req.base_url
            )
        elif req.provider_type == "openai":
            if not req.api_key:
                raise HTTPException(
                    status_code=400,
                    detail="api_key is required to discover OpenAI models",
                )
            meta = await discovery.fetch_openai_models(
                req.api_key,
                base_url=req.base_url,
                organization=req.organization,
            )
        else:  # ollama
            # Admin opted in to a self-hosted Ollama URL — loopback is
            # the legitimate default. The non-admin route never gets
            # here (the admin check above gates the entire handler).
            meta = await discovery.fetch_ollama_models(
                req.base_url, allow_loopback=True
            )
    except UrlSafetyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"upstream error: {e.response.text[:200]}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"{req.provider_type}: {e}")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))

    return {"models": [m.id for m in meta]}


@router.post("/test-connection")
async def test_connection(
    req: TestConnectionRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Stateless pre-save connection test for the Add Provider wizard.

    Admin-only and persists nothing: it probes the provider against the
    credentials in the body. Same trust model as ``/discover-models`` — the
    raw key is accepted in the body, but only an authenticated admin can
    reach it. A static single-segment path, so it never collides with
    ``/{provider_id}/test``.
    """
    _require_settings_admin(current_user)
    _validate_type(req.provider_type)
    success, error = await _probe_provider_connection(
        provider_type=req.provider_type,
        base_url=req.base_url,
        api_key=req.api_key,
        default_model=req.default_model,
        organization=req.organization,
    )
    return {"success": success, "error": error}


@router.get("/{provider_id}/models")
async def list_models(
    provider_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    row = db.get(LLMProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    from services.model_registry import fetch_provider_models

    # ``fetch_provider_models`` delegates to the discovery module and
    # falls back to the cold-boot list on any error, so callers always
    # get a usable payload.
    try:
        models = await fetch_provider_models(row)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))
    return {"models": models}


@router.post("/{provider_id}/refresh-models")
async def refresh_provider_models(
    provider_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Force a live rediscovery for one provider and push the union of
    same-type providers' models to Bifrost's allow-list. Invalidates the
    registry's TTL cache so the next dropdown fetch sees fresh data.
    """
    _require_settings_admin(current_user)
    row = db.get(LLMProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    from services.bifrost_admin import sync_all_provider_models
    from services.model_registry import invalidate_model_cache

    invalidate_model_cache()
    # Run the same union-of-same-type sync used at startup so Bifrost
    # sees the new state too, not just the backend's cache.
    sync_results = await sync_all_provider_models()

    from services.model_registry import fetch_provider_models

    try:
        models = await fetch_provider_models(row)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "provider_id": provider_id,
        "provider_type": row.provider_type,
        "models": models,
        "bifrost_sync": sync_results.get(row.provider_type, False),
    }


@router.post("/refresh-models")
async def refresh_all_provider_models(
    current_user: User = Depends(get_current_active_user),
):
    """Force live rediscovery across every active provider and push the
    resulting allow-lists to Bifrost. Useful after enabling a new
    provider or rotating keys in bulk.
    """
    _require_settings_admin(current_user)
    from services.bifrost_admin import sync_all_provider_models
    from services.model_registry import invalidate_model_cache

    invalidate_model_cache()
    sync_results = await sync_all_provider_models()
    return {"bifrost_sync": sync_results}
