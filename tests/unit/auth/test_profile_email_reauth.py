"""PUT /api/auth/me may change the address password reset mails. That takes the
current password, and a current MFA code when MFA is on. A display-name edit
does not.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import core.storage.unit_of_work as uow_module
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


def _filter(user, args):
    """Email-uniqueness lookups return no row; everything else returns the user."""
    result = MagicMock()
    if any("email" in str(arg) for arg in args):
        result.first.return_value = None
    else:
        result.first.return_value = user
    locked = MagicMock()
    locked.first.return_value = user
    result.populate_existing.return_value.with_for_update.return_value = locked
    return result


@pytest.fixture
def user(password_hash):
    return SimpleNamespace(
        user_id="u-1",
        username="analyst",
        email="a@example.com",
        full_name="Analyst",
        password_hash=password_hash,
        is_verified=True,
        mfa_enabled=False,
        mfa_secret=None,
        mfa_recovery_codes=[],
        mfa_last_totp_step=None,
        failed_login_count=0,
        locked_until=None,
        role_id="role-analyst",
        is_active=True,
        last_login=None,
        login_count=0,
        created_at=None,
        updated_at=None,
    )


@pytest.fixture
def client(user, monkeypatch):
    session = MagicMock()
    session.query.return_value.filter.side_effect = lambda *args, **kwargs: _filter(
        user, args
    )
    counter = MagicMock()
    counter.query.return_value.filter.return_value.first.return_value = user
    monkeypatch.setattr(uow_module, "get_db_session", lambda: counter)
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(auth.router, prefix="/api/auth")
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[request_unit_of_work] = lambda: session
    limiter.reset()
    with patch.object(auth, "revoke_all_for_user", new=AsyncMock()) as revoke:
        test_client = TestClient(app)
        test_client.revoke = revoke
        yield test_client


def _put(client, **kwargs):
    body = {
        key: kwargs[key]
        for key in ("full_name", "email", "current_password", "mfa_code")
        if key in kwargs
    }
    return client.put("/api/auth/me", json=body or None)


def test_name_change_needs_no_password(client, user):
    resp = _put(client, full_name="Ada Lovelace")
    assert resp.status_code == 200
    assert user.full_name == "Ada Lovelace"
    assert user.email == "a@example.com"
    assert user.is_verified is True
    client.revoke.assert_not_awaited()


def test_email_change_without_password_is_refused(client, user):
    resp = _put(client, email=NEW_EMAIL)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Current password is required to change email"
    assert user.email == "a@example.com"
    assert user.is_verified is True
    client.revoke.assert_not_awaited()


def test_email_change_with_wrong_password_is_refused(client, user):
    resp = _put(client, email=NEW_EMAIL, current_password="not-the-password")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Current password is incorrect"
    assert user.email == "a@example.com"


def test_email_change_with_password_updates_and_revokes(client, user):
    resp = _put(client, email=NEW_EMAIL, current_password=CURRENT, full_name="Ada")
    assert resp.status_code == 200
    assert user.email == NEW_EMAIL
    assert user.full_name == "Ada"
    assert user.is_verified is False
    client.revoke.assert_awaited_once_with("u-1")


def test_query_email_with_password_in_the_body(client, user):
    resp = client.put(
        "/api/auth/me",
        params={"email": NEW_EMAIL},
        json={"current_password": CURRENT},
    )
    assert resp.status_code == 200
    assert user.email == NEW_EMAIL


def test_same_address_is_not_a_change(client, user):
    resp = _put(client, email="A@Example.com")
    assert resp.status_code == 200
    assert user.email == "a@example.com"
    assert user.is_verified is True
    client.revoke.assert_not_awaited()


def test_email_change_with_mfa_requires_a_code(client, user):
    user.mfa_enabled = True
    user.mfa_secret = AuthService._encrypt_mfa_secret(pyotp.random_base32())

    resp = _put(client, email=NEW_EMAIL, current_password=CURRENT)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "MFA code required"
    assert user.email == "a@example.com"
    assert user.failed_login_count == 0


def test_email_change_with_mfa_accepts_a_current_code(client, user):
    secret = pyotp.random_base32()
    user.mfa_enabled = True
    user.mfa_secret = AuthService._encrypt_mfa_secret(secret)

    resp = _put(
        client,
        email=NEW_EMAIL,
        current_password=CURRENT,
        mfa_code=pyotp.TOTP(secret).now(),
    )
    assert resp.status_code == 200
    assert user.email == NEW_EMAIL
    client.revoke.assert_awaited_once()


def test_wrong_mfa_code_on_email_change_counts_and_leaves_the_address(client, user):
    user.mfa_enabled = True
    user.mfa_secret = AuthService._encrypt_mfa_secret(pyotp.random_base32())

    resp = _put(client, email=NEW_EMAIL, current_password=CURRENT, mfa_code="000000")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid MFA code"
    assert user.email == "a@example.com"
    assert user.failed_login_count == 1


def test_repeated_wrong_passwords_are_rate_limited(client, user, monkeypatch):
    monkeypatch.setattr(
        auth.AuthService, "verify_password", staticmethod(lambda *a, **k: False)
    )
    codes = [
        _put(client, email=NEW_EMAIL, current_password="nope").status_code
        for _ in range(6)
    ]
    assert codes == [400] * 5 + [429]
    assert user.email == "a@example.com"
