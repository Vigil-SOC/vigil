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
from sqlalchemy import update
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).parent.parent))
from secrets_manager import delete_secret, get_secret, set_secret

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from database.connection import get_db_session
from database.models import LLMProviderConfig
from services.bifrost_admin import push_provider_key

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_PROVIDER_TYPES = {"anthropic", "openai", "ollama"}
_SLUG_RE = re.compile(r"[^a-z0-9-]+")

# Anthropic's live /v1/models endpoint is consulted via
# ``services.provider_model_discovery``; the fallback tuple here is the
# cold-boot list used only when the live call fails (e.g. no API key
# was provided at /discover-models time).
from services.model_registry import _FALLBACK_MODELS_BY_PROVIDER

ANTHROPIC_FALLBACK_MODELS = list(_FALLBACK_MODELS_BY_PROVIDER["anthropic"])


def _slugify(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-") or "provider"


def _secret_ref_for(provider_id: str) -> str:
    return f"llm_provider_{provider_id}_api_key"


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

    The DB has a partial unique index too, but clearing first avoids the
    index-conflict round-trip on UPDATE.
    """
    db.execute(
        update(LLMProviderConfig)
        .where(
            LLMProviderConfig.provider_type == provider_type,
            LLMProviderConfig.provider_id != keep_id,
            LLMProviderConfig.is_default.is_(True),
        )
        .values(is_default=False)
    )


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
async def list_providers(db: Session = Depends(get_db_session)):
    rows = db.query(LLMProviderConfig).order_by(LLMProviderConfig.created_at).all()
    return [_to_response(r) for r in rows]


@router.post("", response_model=LLMProviderResponse, status_code=201)
@router.post("/", response_model=LLMProviderResponse, status_code=201)
async def create_provider(
    payload: LLMProviderCreate, db: Session = Depends(get_db_session)
):
    _validate_type(payload.provider_type)

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
    db: Session = Depends(get_db_session),
):
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
            # Clearing the key: push an empty value to Bifrost so it stops
            # trying to authenticate with a stale credential.
            push_provider_key(row.provider_type, "")
        else:
            if not set_secret(ref, payload.api_key):
                raise HTTPException(status_code=500, detail="Failed to persist API key")
            row.api_key_ref = ref
            push_provider_key(row.provider_type, payload.api_key)

    if payload.is_default is True:
        row.is_default = True
        _clear_other_defaults(db, row.provider_type, provider_id)
    elif payload.is_default is False:
        row.is_default = False

    db.commit()
    db.refresh(row)
    _schedule_catalog_resync(f"updated provider {provider_id}")
    return _to_response(row)


@router.delete("/{provider_id}")
async def delete_provider(provider_id: str, db: Session = Depends(get_db_session)):
    row = db.get(LLMProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    if row.api_key_ref:
        try:
            delete_secret(row.api_key_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to delete secret %s: %s", row.api_key_ref, exc)
        # Wipe the corresponding value on Bifrost too.
        push_provider_key(row.provider_type, "")

    db.delete(row)
    db.commit()
    _schedule_catalog_resync(f"deleted provider {provider_id}")
    return {"success": True, "provider_id": provider_id}


@router.post("/{provider_id}/set-default", response_model=LLMProviderResponse)
async def set_default_provider(provider_id: str, db: Session = Depends(get_db_session)):
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


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str, db: Session = Depends(get_db_session)):
    row = db.get(LLMProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    from datetime import datetime

    success = False
    error: Optional[str] = None

    try:
        if row.provider_type == "ollama":
            base_url = row.base_url or "http://localhost:11434"
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
                resp.raise_for_status()
                success = True
        elif row.provider_type == "openai":
            base_url = row.base_url or "https://api.openai.com/v1"
            key = await _resolve_api_key(row)
            if not key:
                raise RuntimeError("no api key configured")
            headers = {"Authorization": f"Bearer {key}"}
            if row.config and row.config.get("organization"):
                headers["OpenAI-Organization"] = row.config["organization"]
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{base_url.rstrip('/')}/models", headers=headers
                )
                resp.raise_for_status()
                success = True
        elif row.provider_type == "anthropic":
            key = await _resolve_api_key(row)
            if not key:
                raise RuntimeError("no api key configured")
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:
                raise RuntimeError("anthropic SDK not installed") from exc
            # Must use AsyncAnthropic (not Anthropic) — this endpoint is
            # async and the sync client would block the FastAPI event loop
            # for up to the full timeout on an invalid key or slow network.
            client = AsyncAnthropic(api_key=key, timeout=15.0)
            await client.messages.create(
                model=row.default_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            success = True
        else:
            raise RuntimeError(f"unsupported provider_type: {row.provider_type}")
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        logger.info("Provider test failed for %s: %s", provider_id, error)

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


@router.post("/discover-models")
async def discover_models(req: DiscoverModelsRequest):
    """Pre-save model discovery for the Add Provider dialog.

    Delegates to ``services.provider_model_discovery``. Returns a flat
    list of model IDs (unchanged contract). For Anthropic, falls back
    to the hard-coded cold-boot list when no key is supplied so the
    dialog still has something to render.
    """
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
            meta = await discovery.fetch_anthropic_models(req.api_key)
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
            meta = await discovery.fetch_ollama_models(req.base_url)
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


@router.get("/{provider_id}/models")
async def list_models(provider_id: str, db: Session = Depends(get_db_session)):
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
    provider_id: str, db: Session = Depends(get_db_session)
):
    """Force a live rediscovery for one provider and push the union of
    same-type providers' models to Bifrost's allow-list. Invalidates the
    registry's TTL cache so the next dropdown fetch sees fresh data.
    """
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
async def refresh_all_provider_models():
    """Force live rediscovery across every active provider and push the
    resulting allow-lists to Bifrost. Useful after enabling a new
    provider or rotating keys in bulk.
    """
    from services.bifrost_admin import sync_all_provider_models
    from services.model_registry import invalidate_model_cache

    invalidate_model_cache()
    sync_results = await sync_all_provider_models()
    return {"bifrost_sync": sync_results}
