"""Page-extension host endpoints.

Mints the short-lived, user-scoped session token a connector's BFF requires, so
the shared signing secret stays server-side. The ``{integration_id}`` route is
generic — no per-connector specifics.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.middleware.auth import get_current_active_user
from database.models import User
from services import extension_session_service as ext_session

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{integration_id}/session-token")
async def get_extension_session_token(
    integration_id: str,
    current_user: User = Depends(get_current_active_user),
):
    """Mint a short-lived session token for an extension's connector BFF."""
    username = (
        getattr(current_user, "username", None)
        or getattr(current_user, "email", None)
        or "unknown"
    )
    try:
        return await ext_session.mint_session_token(integration_id, username)
    except ext_session.ExtensionSessionError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
