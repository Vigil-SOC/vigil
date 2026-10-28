"""GET /api/llm/health — the providers map the status line folds.

Every built-in role, on ``platform.read`` like ``/api/processes``. Kept off the Bifrost proxy: that router requires
``settings.write``, and its catch-all would take a path mounted there.
The body is only ``providers``. When the refresh could not reach Bifrost
the answer is 200 and an empty map, so the line can say "No routable provider."
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter
from pydantic import BaseModel

from core.auth.permissions import permission_gate
from core.llm.bifrost import path_health
from core.routing import Auth, RouterMeta

router = APIRouter(dependencies=[permission_gate("platform.read")])

ROUTER_META = RouterMeta(
    prefix="/api/llm/health",
    tags=["llm-health"],
    auth=Auth.REQUIRED,
)


class LlmPathHealth(BaseModel):
    """Whether each provider can route. No key id, name, or budget."""

    providers: Dict[str, bool]


@router.get("", response_model=LlmPathHealth)
async def llm_path_health() -> Dict[str, Any]:
    """Per-provider routability for the status line."""
    snap = await path_health.read()
    try:
        body = path_health.routability_body(snap)
    except path_health.UNREACHABLE:
        return {"providers": {}}
    providers = body.get("providers") or {}
    return {
        "providers": {str(name): bool(flag) for name, flag in providers.items() if name}
    }
