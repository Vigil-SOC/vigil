"""The refresh handler's sign-in step.

A refresh takes a row lock on the sign-in that lasts until the request commits.
The lock, the expiry update and the user payload all happen in one synchronous
helper so the handler never awaits while holding it (a refresh stampede on one
sign-in otherwise starves the thread pool the lock holder needs to finish).
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from services.api.routers import auth as auth_router

pytestmark = pytest.mark.unit

EXPIRES = datetime(2030, 1, 1, tzinfo=timezone.utc)


def _user(principal_id="p-1"):
    user = MagicMock()
    user.principal_id = principal_id
    user.user_id = "u-1"
    return user


@pytest.fixture
def payload(monkeypatch):
    monkeypatch.setattr(
        auth_router, "_user_payload", lambda user, session: {"user_id": user.user_id}
    )


def test_live_sign_in_is_extended_and_payload_returned(payload):
    row = MagicMock(ended_at=None, principal_id="p-1", expires_at=None)
    session = MagicMock()
    session.get.return_value = row

    result = auth_router._continue_sign_in(session, "si-1", _user(), EXPIRES)

    assert result == {"user_id": "u-1"}
    assert row.expires_at == EXPIRES
    assert session.get.call_args.kwargs == {"with_for_update": True}


def test_ended_sign_in_is_refused(payload):
    row = MagicMock(ended_at=datetime.now(timezone.utc), principal_id="p-1")
    session = MagicMock()
    session.get.return_value = row

    assert auth_router._continue_sign_in(session, "si-1", _user(), EXPIRES) is None


def test_sign_in_of_another_principal_is_refused(payload):
    row = MagicMock(ended_at=None, principal_id="someone-else")
    session = MagicMock()
    session.get.return_value = row

    assert auth_router._continue_sign_in(session, "si-1", _user(), EXPIRES) is None


def test_token_without_sid_takes_no_lock(payload):
    session = MagicMock()

    result = auth_router._continue_sign_in(session, None, _user(), EXPIRES)

    assert result == {"user_id": "u-1"}
    session.get.assert_not_called()
