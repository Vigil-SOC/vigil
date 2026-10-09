"""Per-component AI model assignment API (GH #89).

Endpoints (registered under /api/ai):
  GET    /config                 — all component → model assignments
  PUT    /config/{component}     — upsert one assignment
  DELETE /config/{component}     — clear one assignment (falls back to chat_default)
  GET    /models                 — aggregated model list across active providers
  GET    /models/{model_id}/info — capability + pricing detail for one model
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.agents.builtins import blank_model
from core.llm.providers.registry import (
    COMPONENTS,
    FALLBACK_KEY,
    ModelInfo,
    catalogue_of,
    get_registry,
    is_valid_component,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import AIModelConfig, ConfigAuditLog, LLMProviderConfig, User
from services.api.middleware.auth import get_current_active_user

logger = logging.getLogger(__name__)
router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/ai",
    tags=["ai-config"],
    auth=Auth.REQUIRED,
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ComponentAssignmentResponse(BaseModel):
    component: str
    provider_id: str
    model_id: str
    settings: Dict[str, Any] = Field(default_factory=dict)
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class ComponentAssignmentUpdate(BaseModel):
    provider_id: str
    model_id: str
    settings: Dict[str, Any] = Field(default_factory=dict)


class AIConfigResponse(BaseModel):
    components: List[str]
    assignments: Dict[str, ComponentAssignmentResponse]


class ModelInfoResponse(BaseModel):
    model_id: str
    provider_id: str
    provider_type: str
    display_name: str
    context_window: int
    input_cost_per_1k: float
    output_cost_per_1k: float
    supports_tools: bool
    supports_thinking: bool
    supports_vision: bool


class ModelsListResponse(BaseModel):
    models: List[ModelInfoResponse]


def _pair(row: Optional[AIModelConfig]) -> Optional[Dict[str, str]]:
    """What the audit diffs: provider and model, plus the fallback when set."""
    if row is None:
        return None
    pair = {"provider_id": row.provider_id, "model_id": row.model_id}
    fallback = blank_model((row.settings or {}).get(FALLBACK_KEY))
    if fallback:
        pair[FALLBACK_KEY] = fallback
    return pair


def _settings_with_fallback(
    payload: ComponentAssignmentUpdate, row: Optional[AIModelConfig]
) -> Dict[str, Any]:
    """The settings to store. A payload that omits the fallback keeps the stored
    one (so a model-only PUT can't wipe it) unless the provider changed, which
    would make it cross-provider. An explicit null or blank clears it."""
    settings = dict(payload.settings)
    if FALLBACK_KEY in settings:
        fallback = blank_model(settings[FALLBACK_KEY])
    elif row is not None and row.provider_id == payload.provider_id:
        fallback = blank_model((row.settings or {}).get(FALLBACK_KEY))
    else:
        fallback = None
    # Never cross-provider, even when the payload carries one.
    if row is not None and row.provider_id != payload.provider_id:
        fallback = None
    # A model change can land on the stored fallback; that's no fallback at all.
    if fallback == payload.model_id and FALLBACK_KEY not in payload.settings:
        fallback = None
    settings.pop(FALLBACK_KEY, None)
    if fallback:
        if fallback == payload.model_id:
            raise HTTPException(
                status_code=400, detail="fallback must differ from the model"
            )
        known = catalogue_of(payload.provider_id)
        if known and fallback not in known:
            raise HTTPException(
                status_code=400,
                detail=f"provider {payload.provider_id} cannot serve {fallback}",
            )
        settings[FALLBACK_KEY] = fallback
    return settings


def _audit(
    db: Any,
    component: str,
    action: str,
    before: Optional[Dict[str, str]],
    after: Optional[Dict[str, str]],
    actor: str,
) -> None:
    """One config_audit_log row on the request's session, so it commits or rolls
    back with the change (ConfigService.record_audit opens its own session)."""
    db.add(
        ConfigAuditLog(
            config_type="ai_model",
            config_key=component,
            action=action,
            old_value=before,
            new_value=after,
            changed_by=actor,
        )
    )


# ---------------------------------------------------------------------------
# Endpoints — config CRUD
# ---------------------------------------------------------------------------


@router.get("/config", response_model=AIConfigResponse)
def get_ai_config(db: UnitOfWorkSession):
    rows = db.query(AIModelConfig).all()
    assignments = {
        r.component: ComponentAssignmentResponse(
            component=r.component,
            provider_id=r.provider_id,
            model_id=r.model_id,
            settings=r.settings or {},
            updated_by=r.updated_by,
            updated_at=r.updated_at.isoformat() if r.updated_at else None,
        )
        for r in rows
    }
    return AIConfigResponse(components=list(COMPONENTS), assignments=assignments)


@router.put("/config/{component}", response_model=ComponentAssignmentResponse)
def set_component_assignment(
    component: str,
    payload: ComponentAssignmentUpdate,
    db: UnitOfWorkSession,
    current_user: User = Depends(get_current_active_user),
):
    if not is_valid_component(component):
        raise HTTPException(status_code=400, detail=f"unknown component: {component}")

    provider = db.get(LLMProviderConfig, payload.provider_id)
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail=f"provider not found: {payload.provider_id}",
        )
    if not provider.is_active:
        raise HTTPException(
            status_code=400,
            detail=f"provider {payload.provider_id} is not active",
        )

    actor = str(current_user.user_id)
    row = db.get(AIModelConfig, component)
    before = _pair(row)
    settings = _settings_with_fallback(payload, row)
    if row is None:
        row = AIModelConfig(
            component=component,
            provider_id=payload.provider_id,
            model_id=payload.model_id,
            settings=settings,
            updated_by=actor,
        )
        db.add(row)
    else:
        row.provider_id = payload.provider_id
        row.model_id = payload.model_id
        row.settings = settings
        row.updated_by = actor
    after = _pair(row)
    if before != after:
        _audit(db, component, "update" if before else "create", before, after, actor)
    # Flush so server-side defaults (updated_at) land before we read them back;
    # the request's unit of work owns the commit.
    db.flush()
    db.refresh(row)

    return ComponentAssignmentResponse(
        component=row.component,
        provider_id=row.provider_id,
        model_id=row.model_id,
        settings=row.settings or {},
        updated_by=row.updated_by,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
    )


@router.delete("/config/{component}")
def clear_component_assignment(
    component: str,
    db: UnitOfWorkSession,
    current_user: User = Depends(get_current_active_user),
):
    if not is_valid_component(component):
        raise HTTPException(status_code=400, detail=f"unknown component: {component}")
    row = db.get(AIModelConfig, component)
    if row is None:
        return {"component": component, "cleared": False}
    _audit(db, component, "delete", _pair(row), None, str(current_user.user_id))
    db.delete(row)
    return {"component": component, "cleared": True}


# ---------------------------------------------------------------------------
# Endpoints — model discovery
# ---------------------------------------------------------------------------


@router.get("/models", response_model=ModelsListResponse)
async def list_models():
    registry = get_registry()
    models: List[ModelInfo] = await registry.list_available_models()
    return ModelsListResponse(models=[ModelInfoResponse(**m.to_dict()) for m in models])


@router.get("/models/{model_id}/info", response_model=ModelInfoResponse)
async def get_model_info(model_id: str, provider_id: Optional[str] = None):
    registry = get_registry()
    # Find the provider for this model. If provider_id is given, trust it;
    # otherwise pick the first active provider that offers the model in its
    # live list (or matches a catalog entry).
    all_models = await registry.list_available_models()
    match: Optional[ModelInfo] = None
    for m in all_models:
        if m.model_id == model_id and (
            provider_id is None or m.provider_id == provider_id
        ):
            match = m
            break
    if match is None:
        raise HTTPException(status_code=404, detail=f"model not found: {model_id}")
    return ModelInfoResponse(**match.to_dict())
