"""Budgets API — read/write Vigil's Bifrost VK config + show live quota.

Lives at ``/api/analytics/budget*`` (mounted under analytics so the cost
dashboard can call it from the same axios client as the other cost
endpoints).

This is the read/write surface for the Settings → LLM Providers →
Budgets sub-panel. Three endpoints:

* ``GET  /api/analytics/budget``       — current persisted settings
                                          (default_vk, ceiling, mode).
* ``PUT  /api/analytics/budget``       — admin-intent write of the same.
* ``GET  /api/analytics/budget/quota`` — live spend/quota for the
                                          configured VK, proxied from
                                          Bifrost's governance API.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
logger = logging.getLogger(__name__)


class BudgetSettingsResponse(BaseModel):
    default_vk: str = ""
    budget_limit_usd: float = 0.0
    enforcement_mode: str = "warning"


class BudgetSettingsUpdate(BaseModel):
    """Admin-intent body for PUT /budget. All fields required so the API
    can't be used to silently drop a setting via an empty PATCH."""

    default_vk: str = Field(default="")
    budget_limit_usd: float = Field(default=0.0, ge=0)
    enforcement_mode: str = Field(default="warning")


@router.get("/analytics/budget", response_model=BudgetSettingsResponse)
async def get_budget_settings() -> Dict[str, Any]:
    """Return the persisted Bifrost VK + budget config."""
    from services.budget_service import get_settings

    return get_settings()


@router.put("/analytics/budget", response_model=BudgetSettingsResponse)
async def put_budget_settings(payload: BudgetSettingsUpdate) -> Dict[str, Any]:
    """Update the Bifrost VK + budget config.

    Admin operation. The dispatch path picks up the change on the next
    runtime-config TTL tick (~60s). When ``DEV_MODE=true`` or
    ``LLM_BUDGET_UNLIMITED=true`` is set, the dispatch ignores the VK
    regardless of what's stored here.
    """
    from services.budget_service import set_settings

    try:
        return set_settings(
            default_vk=payload.default_vk.strip(),
            budget_limit_usd=payload.budget_limit_usd,
            enforcement_mode=payload.enforcement_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("budget settings write failed: %s", e)
        raise HTTPException(status_code=500, detail="Failed to persist settings")


@router.get("/analytics/budget/quota")
async def get_budget_quota() -> Dict[str, Any]:
    """Live spend/quota for the configured VK, proxied from Bifrost."""
    from services.bifrost_cost_client import get_vk_quota
    from services.budget_service import get_active_vk

    vk = get_active_vk()
    if not vk:
        return {
            "configured": False,
            "message": (
                "No Bifrost virtual key configured. Set one in Settings → "
                "LLM Providers → Budgets to enable spend tracking."
            ),
        }

    quota = get_vk_quota(vk)
    if quota is None:
        # Soft failure — Bifrost unreachable or VK invalid. The settings
        # endpoint is still useful (lets the operator fix the config),
        # so we surface a 200 with diagnostic info instead of 502.
        return {
            "configured": True,
            "virtual_key_id": vk,
            "available": False,
            "message": (
                "Could not reach Bifrost or VK was rejected. Check that "
                "Bifrost is up and the configured VK exists in Bifrost's "
                "governance settings."
            ),
        }

    return {
        "configured": True,
        "available": True,
        "virtual_key_id": vk,
        "quota": quota,
    }
