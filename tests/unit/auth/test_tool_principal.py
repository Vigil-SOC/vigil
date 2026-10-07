"""The principal a chat turn carries is the API's word, and nothing more.

It travels through the agent layer, so what matters is what that layer cannot do
with it: forge one, keep using an old one, or present it as a session.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest
from fastapi import HTTPException

from core.auth import auth_service, tool_principal
from core.auth.current_user import get_current_user

pytestmark = pytest.mark.unit


def test_a_minted_principal_verifies_to_the_username():
    assert tool_principal.verify(tool_principal.mint("nestor")) == "nestor"


def test_an_expired_principal_is_refused():
    stale = tool_principal.mint("nestor", ttl=timedelta(minutes=-2))
    with pytest.raises(tool_principal.InvalidPrincipal):
        tool_principal.verify(stale)


@pytest.mark.parametrize(
    "forged",
    [
        # Signed with the session key itself, not the derived one.
        lambda: jwt.encode(
            {"sub": "admin", "aud": "vigil-tool-principal", "exp": 9999999999},
            auth_service.JWT_SECRET_KEY,
            algorithm="HS256",
        ),
        # Signed with a key the agent layer holds.
        lambda: jwt.encode(
            {"sub": "admin", "aud": "vigil-tool-principal", "exp": 9999999999},
            "the-internal-token",
            algorithm="HS256",
        ),
        # A bare name.
        lambda: "admin",
    ],
    ids=["session-key", "other-key", "bare-name"],
)
def test_a_principal_the_api_did_not_sign_is_refused(forged):
    with pytest.raises(tool_principal.InvalidPrincipal):
        tool_principal.verify(forged())


def test_a_session_jwt_is_not_a_principal():
    user = SimpleNamespace(
        user_id="u-1", username="nestor", email="n@example.com", role_id="r"
    )
    session_token = auth_service.AuthService.generate_jwt_token(user)
    with pytest.raises(tool_principal.InvalidPrincipal):
        tool_principal.verify(session_token)


@pytest.mark.asyncio
async def test_a_principal_does_not_open_the_api(monkeypatch):
    """The agent layer holds one; it must not hold a session for that analyst."""
    monkeypatch.setattr("core.auth.current_user.DEV_MODE", False)
    request = MagicMock()
    request.cookies = {}

    with pytest.raises(HTTPException) as refused:
        await get_current_user(
            request,
            f"Bearer {tool_principal.mint('nestor')}",
            session=MagicMock(),
        )
    assert refused.value.status_code == 401
    # Refused at the signature, not later at a user lookup a session would reach.
    assert refused.value.detail == "Invalid or expired token"
