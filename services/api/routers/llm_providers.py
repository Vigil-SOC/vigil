"""LLM provider management API — CRUD.

See GH issue #88. Each row in `llm_provider_configs` represents a configured
LLM backend (Anthropic, OpenAI, Ollama, ...). API keys are stored in the
secrets_manager under a generated ref name, never in the DB.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.llm.bifrost.admin import (
    push_provider_key,
    sync_all_provider_models,
)
from core.llm.providers import provider_service
from core.llm.providers.base_url import validate_base_url_shape
from core.llm.providers.registry import (
    VALID_PROVIDER_TYPES,
    attest_self_hosted,
    invalidate_model_cache,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.secrets import delete_secret, secret_write_failure_detail, set_secret
from core.storage.models import LLMProviderConfig, User
from core.storage.schemas import LLMProviderConfigSchema
from services.api.middleware.auth import get_current_active_user, require_settings_admin

logger = logging.getLogger(__name__)
router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/llm/providers",
    tags=["llm-providers"],
    auth=Auth.REQUIRED,
)

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-") or "provider"


def _secret_ref_for(provider_id: str) -> str:
    return f"llm_provider_{provider_id}_api_key"


def _validate_provider_base_url_shape(base_url: Optional[str]) -> None:
    """Shape-only check (see ``validate_base_url_shape``), as a 400."""
    try:
        validate_base_url_shape(base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
    d = LLMProviderConfigSchema.dump(row)
    d.pop("api_key_ref", None)
    return d


def _validate_type(provider_type: str) -> None:
    if provider_type not in VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"provider_type must be one of {sorted(VALID_PROVIDER_TYPES)}",
        )


def _refresh_self_hosted_attestation(db: UnitOfWorkSession) -> None:
    """Apply ``config.self_hosted`` to this process's pricing now, not on the
    next refresh. The worker and daemon converge on their own refresh interval."""
    attest_self_hosted(provider_service.active_provider_configs(db))


def _schedule_catalog_resync(reason: str) -> None:
    """Invalidate the model-list cache and fire a background sync.

    Called from provider CRUD so the UI dropdown and Bifrost's allow-list
    reflect the change immediately, rather than waiting up to the next
    scheduled refresh. Best-effort — never blocks the response.
    """
    import asyncio

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
    db: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    rows = provider_service.list_providers(db)
    return [_to_response(r) for r in rows]


@router.post("", response_model=LLMProviderResponse, status_code=201)
@router.post("/", response_model=LLMProviderResponse, status_code=201)
async def create_provider(
    payload: LLMProviderCreate,
    db: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    require_settings_admin(current_user)
    _validate_type(payload.provider_type)
    _validate_provider_base_url_shape(payload.base_url)

    provider_id = payload.provider_id or _slugify(payload.name)
    if provider_service.get_provider(db, provider_id) is not None:
        raise HTTPException(
            status_code=409, detail=f"provider_id '{provider_id}' already exists"
        )

    api_key_ref: Optional[str] = None
    if payload.api_key:
        api_key_ref = _secret_ref_for(provider_id)
        if not set_secret(api_key_ref, payload.api_key):
            raise HTTPException(
                status_code=500,
                detail=secret_write_failure_detail("Failed to persist API key"),
            )
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
    provider_service.insert(db, row, make_default=payload.is_default)
    _refresh_self_hosted_attestation(db)
    _schedule_catalog_resync(f"created provider {provider_id}")
    return _to_response(row)


@router.put("/{provider_id}", response_model=LLMProviderResponse)
async def update_provider(
    provider_id: str,
    payload: LLMProviderUpdate,
    db: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    require_settings_admin(current_user)
    _validate_provider_base_url_shape(payload.base_url)
    row = provider_service.get_provider(db, provider_id)
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
        # The write replaces config, but must not erase the marker that makes a
        # mirror row a mirror row.
        managed = {k: v for k, v in (row.config or {}).items() if k == "managed_by"}
        row.config = {**managed, **payload.config}

    if payload.api_key is not None:
        # Empty string clears the key; non-empty rotates it.
        ref = row.api_key_ref or _secret_ref_for(provider_id)
        if payload.api_key == "":
            delete_secret(ref)
            row.api_key_ref = None
            # Bifrost's key is shared per provider_type, so only blank it if
            # no same-type sibling still has a key — otherwise re-push the
            # survivor's so the type keeps a working credential.
            provider_service.reconcile_bifrost_key_for_type(
                db, row.provider_type, provider_id
            )
        else:
            if not set_secret(ref, payload.api_key):
                raise HTTPException(
                    status_code=500,
                    detail=secret_write_failure_detail("Failed to persist API key"),
                )
            row.api_key_ref = ref
            push_provider_key(row.provider_type, payload.api_key)

    if payload.is_default is True:
        row.is_default = True
        provider_service.clear_other_defaults(db, row.provider_type, provider_id)
    elif payload.is_default is False:
        if row.is_default:
            # Guard: only enforce when transitioning True → False.
            other_default = provider_service.find_other_default(
                db, row.provider_type, provider_id
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

    provider_service.settle(db, row)
    _refresh_self_hosted_attestation(db)
    _schedule_catalog_resync(f"updated provider {provider_id}")
    return _to_response(row)


@router.delete("/{provider_id}")
async def delete_provider(
    provider_id: str,
    db: UnitOfWorkSession,
    current_user: Annotated[User, Depends(get_current_active_user)],
):
    require_settings_admin(current_user)
    row = provider_service.get_provider(db, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    # Guard: refuse to delete the only active default for this provider type.
    if row.is_default:
        other_active = provider_service.find_other_active(
            db, row.provider_type, provider_id
        )
        if other_active is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Cannot delete the only active {row.provider_type} provider. "
                    "Add or activate another provider of this type first."
                ),
            )
        provider_service.demote_and_promote(db, row, other_active)

    # Cascade: remove model-config rows that reference this provider before
    # deleting — the FK is ON DELETE RESTRICT so this must happen first.
    provider_service.delete_model_configs_for_provider(db, provider_id)

    if row.api_key_ref:
        try:
            delete_secret(row.api_key_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to delete secret %s: %s", row.api_key_ref, exc)
        # Bifrost's key is shared per provider_type. Only wipe it if this was
        # the last keyed provider of its type; otherwise re-push a surviving
        # sibling's key so the type isn't left without a working credential.
        # (``row`` is still in the session here — exclude it explicitly.)
        provider_service.reconcile_bifrost_key_for_type(
            db, row.provider_type, provider_id
        )

    provider_service.remove(db, row)
    _refresh_self_hosted_attestation(db)
    _schedule_catalog_resync(f"deleted provider {provider_id}")
    return {"success": True, "provider_id": provider_id}
