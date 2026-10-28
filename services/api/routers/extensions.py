"""Page-extension host endpoints.

Mints the short-lived, user-scoped session token a connector's BFF requires, so
the shared signing secret stays server-side. The ``{integration_id}`` route is
generic — no per-connector specifics.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth.permissions import deny_unless
from core.integrations.extension import session_service as ext_session
from core.routing import Auth, RouterMeta
from core.storage.models import User
from services.api.middleware.auth import get_current_active_user

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/integrations",
    tags=["extensions"],
    auth=Auth.ROUTER_MANAGED,
    reason=(
        "Page-extension host that mints connector session tokens. Each "
        "handler declares Depends(get_current_active_user) individually "
        "rather than inheriting a router-level dependency."
    ),
)


@router.get("/{integration_id}/session-token")
async def get_extension_session_token(
    integration_id: str,
    current_user: User = Depends(get_current_active_user),
):
    """Mint a short-lived session token for an extension's connector BFF."""
    # Mirror the connector's manifest `mountPoint.permission` server-side by
    # convention (`<id>.view`), so the API enforces the same RBAC gate the UI
    # and the loglm.view grant do — otherwise any authenticated user could mint.
    deny_unless(current_user.user_id, f"{integration_id}.view")
    username = (
        getattr(current_user, "username", None)
        or getattr(current_user, "email", None)
        or "unknown"
    )
    try:
        return await ext_session.mint_session_token(integration_id, username)
    except ext_session.ExtensionSessionError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
