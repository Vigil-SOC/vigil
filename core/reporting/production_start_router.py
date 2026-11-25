"""Production Start, read under ``platform.read`` and written under ``platform.write``.

Beside the Day 1 routes, not under ``/api/v1``. Declaring and clearing are the
same write: a null ``started_at`` stores both times as null.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth.permissions import require_permission
from core.reporting.production import Rejected, declare, read
from core.routing import Auth, RouterMeta
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/production-start",
    tags=["production-start"],
    auth=Auth.REQUIRED,
)


class ProductionStart(BaseModel):
    started_at: Optional[str] = None
    declared_at: Optional[str] = None


class ProductionStartWrite(BaseModel):
    # Null clears. The server sets declared_at; a client value for it is ignored.
    started_at: Optional[datetime]


@router.get("", response_model=ProductionStart)
def production_start(
    _user: User = Depends(require_permission("platform.read")),
) -> ProductionStart:
    """The declared production start, or both times null when unset."""
    return ProductionStart.model_validate(read())


@router.put("", response_model=ProductionStart)
def set_production_start(
    body: ProductionStartWrite,
    user: User = Depends(require_permission("platform.write")),
) -> ProductionStart:
    """Declare a start of now or earlier, change it, or clear it."""
    try:
        saved = declare(body.started_at, str(user.user_id))
    except Rejected as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from None
    if saved is None:
        raise HTTPException(
            status_code=500, detail="Could not save the production start."
        )
    return ProductionStart.model_validate(saved)
