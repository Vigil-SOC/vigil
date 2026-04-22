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
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from database.connection import get_db_session  # noqa: E402
from database.models import AIModelConfig, LLMProviderConfig  # noqa: E402
from services.model_registry import (  # noqa: E402
    COMPONENTS,
    ModelInfo,
    get_registry,
    is_valid_component,
)

logger = logging.getLogger(__name__)
router = APIRouter()


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


# ---------------------------------------------------------------------------
# Endpoints — config CRUD
# ---------------------------------------------------------------------------


@router.get("/config", response_model=AIConfigResponse)
def get_ai_config(db: Session = Depends(get_db_session)):
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
    db: Session = Depends(get_db_session),
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

    row = db.get(AIModelConfig, component)
    if row is None:
        row = AIModelConfig(
            component=component,
            provider_id=payload.provider_id,
            model_id=payload.model_id,
            settings=payload.settings,
        )
        db.add(row)
    else:
        row.provider_id = payload.provider_id
        row.model_id = payload.model_id
        row.settings = payload.settings
    db.commit()
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
def clear_component_assignment(component: str, db: Session = Depends(get_db_session)):
    if not is_valid_component(component):
        raise HTTPException(status_code=400, detail=f"unknown component: {component}")
    row = db.get(AIModelConfig, component)
    if row is None:
        return {"component": component, "cleared": False}
    db.delete(row)
    db.commit()
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
