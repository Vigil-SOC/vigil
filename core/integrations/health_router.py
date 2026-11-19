"""GET /api/integration-health: every source's Integration Health record and state.

The state is computed by :func:`core.integrations.health.judge`, the only place
it is worked out. A failed store read answers 503, never a list of healthy
or empty sources.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from core.auth.permissions import permission_gate
from core.integrations import health
from core.routing import Auth, RouterMeta
from core.storage.schemas.base import _iso_utc
from core.time import utcnow

logger = logging.getLogger(__name__)

router = APIRouter()
router.dependencies.append(permission_gate("integrations.read"))

ROUTER_META = RouterMeta(
    prefix="/api/integration-health",
    tags=["integration-health"],
    auth=Auth.REQUIRED,
)


def _utc(value: Any) -> Any:
    """Naive UTC datetimes as isoformat with +00:00, the API's datetime contract."""
    if isinstance(value, datetime):
        return _iso_utc(value)
    if isinstance(value, dict):
        return {k: _utc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_utc(v) for v in value]
    return value


@router.get("")
def integration_health() -> Dict[str, Any]:
    now = utcnow()
    try:
        sources = health.collect(now)
    except Exception as exc:
        logger.error("Integration health read failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Could not read integration health"
        ) from exc
    return _utc({"checked_at": now, "sources": sources})
