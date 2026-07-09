"""Page-extension host endpoints.

Vigil's OSS page-extension mechanism (``frontend/src/redesign/extensions``)
lets an opt-in connector contribute a UI page. The connector's web component
calls its own BFF directly from the browser; this router mints the
short-lived, user-scoped session token that BFF requires, so the shared
signing secret stays server-side.

Mounted at ``/api/integrations`` so the path is
``/api/integrations/{integration_id}/session-token`` — the ``{integration_id}``
route is generic (no LogLM specifics), matching the "Vigil knows nothing about
the extension" principle.
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
