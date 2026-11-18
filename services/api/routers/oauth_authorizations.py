"""A person approves or denies an app's request to connect (#4788).

``/authorize`` sends the browser to the console's consent page with a request
id; the page reads and decides it here, with the session cookie (so CSRF
applies). Approving is minting a credential for yourself, so it follows the
minting rules of ``services/api/routers/tokens.py``. Both answers are JSON
holding the client's redirect URL, for the page to follow: never a 302.

Unversioned console routes, outside the ``/api/v1`` contract.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from core.auth import oauth_server
from core.auth.auth_service import _is_service_account
from core.auth.service_account import is_medic
from core.integrations.mcp.surface import is_oauth_enabled
from core.routing import Auth, RouterMeta
from core.storage.models import User
from services.api.middleware.auth import get_current_active_user
from services.api.routers.tokens import _confirm, _refuse_marked, _refuse_token_request

logger = logging.getLogger(__name__)
router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/oauth/authorizations", tags=["oauth"], auth=Auth.REQUIRED
)

_GONE = "This request has expired or was already answered. Start again from the app."


class ConsentDecision(BaseModel):
    decision: Literal["approve", "deny"]


def _enabled() -> None:
    if not is_oauth_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


@router.get("/{request_id}")
async def read(request_id: str, current_user: User = Depends(get_current_active_user)):
    """The client asking, the host it returns to, and the resource it wants."""
    await run_in_threadpool(_enabled)
    try:
        return await run_in_threadpool(oauth_server.pending_request, request_id)
    except oauth_server.PendingNotFound:
        raise HTTPException(status_code=404, detail=_GONE) from None


@router.post("/{request_id}")
def decide(
    request: Request,
    request_id: str,
    body: ConsentDecision,
    current_user: User = Depends(get_current_active_user),
):
    """Approve or deny, as a person signed in to the console. Approving connects
    the app as you, so it asks you to confirm it's you first."""
    _enabled()
    approve = body.decision == "approve"
    _refuse_token_request(request, "answer an app's request")
    if _is_service_account(current_user) or is_medic(current_user):
        raise HTTPException(
            status_code=403, detail="A service account can't answer an app's request"
        )
    if approve:
        _refuse_marked(current_user)
        _confirm(request, current_user, "connect an app")
    claims = getattr(request.state, "access_claims", None) or {}
    try:
        redirect = oauth_server.decide(
            request_id, current_user, approve, claims.get("sid")
        )
    except oauth_server.PendingNotFound:
        raise HTTPException(status_code=404, detail=_GONE) from None
    return {"redirect_to": redirect}
