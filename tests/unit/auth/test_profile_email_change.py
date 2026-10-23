"""PUT /api/auth/me: a session may rename the account, but replacing the email
requires the current password.

Password reset is delivered to the address on the account. The fields are
read from the JSON body, so the password is not placed on the query string.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core.auth.auth_service import AuthService
from core.routing import request_unit_of_work
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth

pytestmark = pytest.mark.unit

CURRENT = "trombone-sunrise-ledger-88"
NEW_EMAIL = "ada@example.com"


@pytest.fixture(scope="module")
def password_hash():
    return AuthService.hash_password(CURRENT)


@pytest.fixture
def user(password_hash):
    return SimpleNamespace(
        user_id="u-1",
        username="analyst",
        email="analyst@example.com",
        full_name="Analyst",
        password_hash=password_hash,
        is_verified=True,
        is_active=True,
        role_id="role-analyst",
        mfa_enabled=False,
        login_count=1,
    )


@pytest.fixture
def session():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    return db


@pytest.fixture
def client(user, session):
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[request_unit_of_work] = lambda: session
    limiter.reset()
    revoke = AsyncMock()
    with patch.object(auth, "revoke_all_for_user", new=revoke):
        yield TestClient(app), revoke


def _put(client, **body):
    return client.put("/api/auth/me", json=body)


def test_email_change_without_password_is_refused(client, user):
    http, revoke = client
    resp = _put(http, email=NEW_EMAIL, full_name="Ada")
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Current password is required to change the email address"
    )
    assert user.email == "analyst@example.com"
    assert user.full_name == "Analyst"
    assert user.is_verified is True
    revoke.assert_not_awaited()


def test_wrong_password_leaves_the_profile_unchanged(client, user):
    http, revoke = client
    resp = _put(http, email=NEW_EMAIL, current_password="not-the-password")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Current password is incorrect"
    assert user.email == "analyst@example.com"
    assert user.is_verified is True
    revoke.assert_not_awaited()


def test_correct_password_changes_email_and_revokes_sessions(client, user):
    http, revoke = client
    resp = _put(
        http, email=NEW_EMAIL, full_name="Ada Lovelace", current_password=CURRENT
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == NEW_EMAIL
    assert resp.json()["full_name"] == "Ada Lovelace"
    assert user.email == NEW_EMAIL
    assert user.is_verified is False
    assert user.tokens_valid_after is not None
    revoke.assert_awaited_once_with(user.user_id)


def test_email_change_stands_when_redis_revocation_fails(client, user):
    http, _revoke = client
    with patch.object(
        auth, "revoke_all_for_user", new=AsyncMock(side_effect=ConnectionError("OOM"))
    ):
        resp = _put(http, email=NEW_EMAIL, current_password=CURRENT)
    assert resp.status_code == 200, resp.text
    assert user.email == NEW_EMAIL
    assert user.tokens_valid_after is not None


def test_same_mailbox_does_not_require_a_password_or_revoke(client, user):
    http, revoke = client
    resp = _put(http, email="Analyst@Example.com")
    assert resp.status_code == 200
    assert user.email == "analyst@example.com"
    assert user.is_verified is True
    revoke.assert_not_awaited()


def test_name_change_does_not_require_a_password(client, user):
    http, revoke = client
    resp = _put(http, full_name="Ada")
    assert resp.status_code == 200
    assert user.full_name == "Ada"
    assert user.email == "analyst@example.com"
    revoke.assert_not_awaited()


def test_query_string_without_a_body_does_not_change_email(client, user):
    http, revoke = client
    resp = http.put("/api/auth/me", params={"email": "attacker@example.com"})
    assert resp.status_code == 422
    assert user.email == "analyst@example.com"
    revoke.assert_not_awaited()


def test_query_string_email_is_rejected(client, user):
    http, revoke = client
    resp = http.put(
        "/api/auth/me?email=attacker@example.com",
        json={"current_password": CURRENT},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Send profile fields in the JSON body"
    assert user.email == "analyst@example.com"
    revoke.assert_not_awaited()


def test_email_already_in_use(client, user, session):
    http, revoke = client
    session.query.return_value.filter.return_value.first.return_value = object()
    resp = _put(http, email=NEW_EMAIL, current_password=CURRENT)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Email already in use"
    assert user.email == "analyst@example.com"
    assert user.is_verified is True
    revoke.assert_not_awaited()


def test_five_wrong_passwords_then_429(client, user):
    http, _revoke = client
    codes = [
        _put(http, email=NEW_EMAIL, current_password="nope").status_code
        for _ in range(6)
    ]
    assert codes == [400] * 5 + [429]
    assert user.email == "analyst@example.com"
