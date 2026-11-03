"""get_current_user releases its DB session before the endpoint runs (#3660)."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.auth import current_user as current_user_module

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_get_current_user_closes_owned_session_before_returning(monkeypatch):
    monkeypatch.setattr(current_user_module, "DEV_MODE", False)
    monkeypatch.setattr(
        current_user_module.AuthService,
        "verify_jwt_token",
        lambda _token: {"user_id": "u-1", "token_type": "access"},
    )
    monkeypatch.setattr(
        current_user_module.AuthService,
        "verify_session_fingerprint",
        lambda _payload, user_agent=None: True,
    )

    async def _not_revoked(_payload):
        return False

    monkeypatch.setattr(current_user_module, "is_token_revoked", _not_revoked)

    user = MagicMock()
    user.user_id = "u-1"
    user.username = "analyst"
    user.principal_id = "p-1"
    user.is_active = True
    user.tokens_valid_after = None
    user.service_account = False

    events: list[str] = []
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = user
    db.__contains__.side_effect = lambda obj: obj is user

    @contextmanager
    def _fake_uow(session=None):
        assert session is None
        events.append("open")
        try:
            yield db
        finally:
            events.append("close")

    monkeypatch.setattr(current_user_module, "unit_of_work", _fake_uow)

    request = SimpleNamespace(
        cookies={"access_token": "tok"},
        headers={"user-agent": "pytest"},
        state=SimpleNamespace(),
    )

    resolved = await current_user_module.get_current_user(request, None, session=None)

    assert resolved is user
    assert events == ["open", "close"]
    db.expunge.assert_called_once_with(user)
