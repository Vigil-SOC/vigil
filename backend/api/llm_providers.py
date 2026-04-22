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

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_PROVIDER_TYPES = {"anthropic", "openai", "ollama"}
_SLUG_RE = re.compile(r"[^a-z0-9-]+")

# Static list for Anthropic — the SDK doesn't expose a /models endpoint
# for listing. Kept in sync with docker/bifrost/config.json.
ANTHROPIC_MODELS = [
    "claude-sonnet-4-20250514",
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-20250514",
]


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
        raise HTTPException(status_code=409, detail=f"provider_id '{provider_id}' already exists")

    api_key_ref: Optional[str] = None
    if payload.api_key:
        api_key_ref = _secret_ref_for(provider_id)
        if not set_secret(api_key_ref, payload.api_key):
            raise HTTPException(status_code=500, detail="Failed to persist API key")

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
        else:
            if not set_secret(ref, payload.api_key):
                raise HTTPException(status_code=500, detail="Failed to persist API key")
            row.api_key_ref = ref

    if payload.is_default is True:
        row.is_default = True
        _clear_other_defaults(db, row.provider_type, provider_id)
    elif payload.is_default is False:
        row.is_default = False

    db.commit()
    db.refresh(row)
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

    db.delete(row)
    db.commit()
    return {"success": True, "provider_id": provider_id}


@router.post("/{provider_id}/set-default", response_model=LLMProviderResponse)
async def set_default_provider(
    provider_id: str, db: Session = Depends(get_db_session)
):
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
    if req.provider_type not in VALID_PROVIDER_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported provider_type: {req.provider_type}",
        )

    if req.provider_type == "anthropic":
        return {"models": ANTHROPIC_MODELS}

    if req.provider_type == "ollama":
        base_url = req.base_url or "http://localhost:11434"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"ollama: {e}")
        return {
            "models": [m.get("name") for m in data.get("models", []) if m.get("name")]
        }

    # openai / openai-compatible
    base_url = req.base_url or "https://api.openai.com/v1"
    if not req.api_key:
        raise HTTPException(
            status_code=400, detail="api_key is required to discover OpenAI models"
        )
    headers = {"Authorization": f"Bearer {req.api_key}"}
    if req.organization:
        headers["OpenAI-Organization"] = req.organization
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        # Surface auth errors cleanly so the dialog can show them
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"upstream error: {e.response.text[:200]}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"openai: {e}")
    return {"models": [m.get("id") for m in data.get("data", []) if m.get("id")]}


@router.get("/{provider_id}/models")
async def list_models(provider_id: str, db: Session = Depends(get_db_session)):
    row = db.get(LLMProviderConfig, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="provider not found")

    if row.provider_type == "anthropic":
        return {"models": ANTHROPIC_MODELS}

    if row.provider_type == "ollama":
        base_url = row.base_url or "http://localhost:11434"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        return {"models": [m.get("name") for m in data.get("models", []) if m.get("name")]}

    if row.provider_type == "openai":
        base_url = row.base_url or "https://api.openai.com/v1"
        key = await _resolve_api_key(row)
        if not key:
            raise HTTPException(status_code=400, detail="no api key configured")
        headers = {"Authorization": f"Bearer {key}"}
        if row.config and row.config.get("organization"):
            headers["OpenAI-Organization"] = row.config["organization"]
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return {"models": [m.get("id") for m in data.get("data", []) if m.get("id")]}

    raise HTTPException(status_code=400, detail=f"unsupported provider_type: {row.provider_type}")
