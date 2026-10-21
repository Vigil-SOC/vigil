"""Wrong second factors share the password lockout.

A correct password does not finish the login when MFA is on. The wrong code
has to land in its own transaction, because the 401 rolls the request back.
The console's first login post omits the code on purpose, so that request
must not count. Enabling and disabling MFA guess the same code and count too.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import core.storage.unit_of_work as uow_module
from core.auth.auth_service import AuthService
from core.routing import request_unit_of_work
from core.time import utcnow
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth

pytestmark = pytest.mark.unit

TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _person(**overrides):
    fields = dict(
        user_id="user-1",
        username="victim",
        email="victim@example.com",
        full_name="Victim",
        role_id="role-admin",
        is_active=True,
        is_verified=True,
        password_hash="hashed",
        mfa_enabled=True,
        mfa_secret=AuthService._encrypt_mfa_secret(TOTP_SECRET),
        mfa_recovery_codes=[],
        failed_login_count=4,
        locked_until=None,
        login_count=3,
        last_login=None,
        created_at=None,
        updated_at=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _wire(session, row):
    session.query.return_value.filter.return_value.first.return_value = row


@pytest.fixture
def user():
    return _person()


@pytest.fixture
def persisted(user):
    """The row the lockout transaction writes. Distinct from the request's copy."""
    return _person(failed_login_count=user.failed_login_count)


@pytest.fixture
def client(user, persisted, monkeypatch):
    session = MagicMock()
    _wire(session, user)
    own = MagicMock()
    _wire(own, persisted)
    monkeypatch.setattr(uow_module, "get_db_session", lambda: own)
    monkeypatch.setattr(
        AuthService, "verify_password", staticmethod(lambda *args, **kwargs: True)
    )
    monkeypatch.setattr(
        AuthService, "get_user_permissions", staticmethod(lambda *args, **kwargs: {})
    )
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[request_unit_of_work] = lambda: session
    limiter.reset()
    return TestClient(app)


def _login(client, **extra):
    body = {"username_or_email": "victim", "password": "correct-horse"}
    body.update(extra)
    return client.post("/api/auth/login", json=body)


def test_wrong_mfa_code_at_login_counts_and_does_not_clear_the_request_row(
    client, user, persisted, caplog
):
    with caplog.at_level("WARNING"):
        resp = _login(client, mfa_code="000000")

    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid MFA code"
    assert user.failed_login_count == 4
    assert persisted.failed_login_count == 5
    assert persisted.locked_until is not None
    assert persisted.locked_until > utcnow()
    assert "Invalid MFA code for user: victim" in caplog.text
    assert "000000" not in caplog.text


def test_login_without_an_mfa_code_does_not_count(client, user, persisted):
    resp = _login(client)

    assert resp.status_code == 401
    assert resp.json()["detail"] == "MFA code required"
    assert resp.headers.get("x-mfa-required") == "true"
    assert user.failed_login_count == 4
    assert persisted.failed_login_count == 4
    assert user.login_count == 3


def test_accepted_mfa_code_clears_lockout_on_the_request(client, user, persisted):
    resp = _login(client, mfa_code=pyotp.TOTP(TOTP_SECRET).now())

    assert resp.status_code == 200
    assert user.failed_login_count == 0
    assert user.locked_until is None
    assert user.login_count == 4
    assert persisted.failed_login_count == 4
    assert "access_token" in resp.json()


def test_locked_account_is_423_before_the_second_factor(client, user, persisted):
    user.locked_until = utcnow() + timedelta(minutes=10)

    resp = _login(client, mfa_code="000000")

    assert resp.status_code == 423
    detail = "Account locked due to repeated failed login attempts"
    assert resp.json()["detail"] == detail
    assert int(resp.headers["retry-after"]) >= 1
    assert persisted.failed_login_count == 4


def test_wrong_code_while_enabling_mfa_counts(client, user, persisted):
    user.mfa_enabled = False

    resp = client.post("/api/auth/mfa/verify", json={"code": "000000"})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid MFA code"
    assert persisted.failed_login_count == 5
    assert persisted.locked_until is not None
    assert user.mfa_enabled is False


def test_enabling_mfa_with_no_secret_does_not_count(client, user, persisted):
    user.mfa_enabled = False
    user.mfa_secret = None

    resp = client.post("/api/auth/mfa/verify", json={"code": "000000"})

    assert resp.status_code == 400
    assert persisted.failed_login_count == 4


def test_enabling_mfa_is_refused_while_the_account_is_locked(client, user, persisted):
    user.mfa_enabled = False
    user.locked_until = utcnow() + timedelta(minutes=10)

    resp = client.post("/api/auth/mfa/verify", json={"code": "000000"})

    assert resp.status_code == 423
    assert persisted.failed_login_count == 4
    assert user.mfa_secret is not None


def test_wrong_code_while_disabling_mfa_counts(client, user, persisted):
    resp = client.request("DELETE", "/api/auth/mfa", json={"code": "000000"})

    assert resp.status_code == 400
    assert user.mfa_enabled is True
    assert persisted.failed_login_count == 5


def test_disabling_mfa_when_it_is_off_does_not_count(client, user, persisted):
    user.mfa_enabled = False

    resp = client.request("DELETE", "/api/auth/mfa", json={"code": "000000"})

    assert resp.status_code == 400
    assert persisted.failed_login_count == 4


def test_disabling_mfa_is_refused_while_the_account_is_locked(client, user, persisted):
    user.locked_until = utcnow() + timedelta(minutes=10)

    resp = client.request("DELETE", "/api/auth/mfa", json={"code": "000000"})

    assert resp.status_code == 423
    assert user.mfa_enabled is True
    assert persisted.failed_login_count == 4
