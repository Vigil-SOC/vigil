"""A wrong MFA code counts toward the same lock as a wrong password.

The login request rolls back on 401, so the counter has to commit on its own
connection, and that connection can only take the row after the code check
releases its FOR UPDATE. A fake session (the route tests) has no second
connection; it must not try to open one.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import core.storage.unit_of_work as uow_module
from core.auth import auth_service as auth_module
from core.auth.auth_service import AuthService
from core.routing import request_unit_of_work
from core.time import utcnow
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth

pytestmark = pytest.mark.unit


def _user(**kwargs):
    user = SimpleNamespace(
        user_id="user-1",
        principal_id="user-1",
        username="victim",
        mfa_secret="secret",
        mfa_enabled=True,
        mfa_recovery_codes=[],
        mfa_last_totp_step=None,
        locked_until=None,
        failed_login_count=0,
    )
    for key, value in kwargs.items():
        setattr(user, key, value)
    return user


def _caller(user):
    session = MagicMock()
    query = session.query.return_value.filter.return_value
    query.populate_existing.return_value.with_for_update.return_value = query
    query.first.return_value = user
    return session


def _nested():
    nested = MagicMock()
    nested.is_active = True

    def rollback():
        nested.is_active = False

    nested.rollback.side_effect = rollback
    return nested


def _own(monkeypatch, user):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = user
    monkeypatch.setattr(uow_module, "get_db_session", lambda: session)
    return session


def test_wrong_code_on_a_real_session_locks_at_the_threshold(monkeypatch):
    monkeypatch.setattr(auth_module, "LOCKOUT_THRESHOLD", 3)
    monkeypatch.setattr(
        AuthService, "_accept_totp", staticmethod(lambda *a, **k: False)
    )
    caller_user = _user()
    own_user = _user(failed_login_count=2)
    nested = _nested()
    monkeypatch.setattr(AuthService, "_lock_savepoint", staticmethod(lambda s: nested))
    own = _own(monkeypatch, own_user)

    assert (
        AuthService.verify_mfa_code("user-1", "000000", _caller(caller_user)) is False
    )

    nested.rollback.assert_called_once()
    assert own_user.failed_login_count == 3
    assert own_user.locked_until is not None
    assert own_user.locked_until > utcnow()
    assert own_user.locked_until <= utcnow() + timedelta(
        minutes=auth_module.LOCKOUT_DURATION_MINUTES
    )
    own.commit.assert_called_once()


def test_wrong_code_on_a_fake_session_does_not_open_a_connection(monkeypatch):
    monkeypatch.setattr(
        AuthService, "_accept_totp", staticmethod(lambda *a, **k: False)
    )

    def boom():
        raise AssertionError("fake session must not open a lockout connection")

    monkeypatch.setattr(uow_module, "get_db_session", boom)

    assert AuthService.verify_mfa_code("user-1", "000000", _caller(_user())) is False


def test_locked_account_is_not_counted_again(monkeypatch):
    monkeypatch.setattr(
        AuthService, "_accept_totp", staticmethod(lambda *a, **k: False)
    )
    nested = _nested()
    monkeypatch.setattr(AuthService, "_lock_savepoint", staticmethod(lambda s: nested))
    user = _user(locked_until=utcnow() + timedelta(minutes=5), failed_login_count=3)

    def boom():
        raise AssertionError("a locked account must not take another count")

    monkeypatch.setattr(uow_module, "get_db_session", boom)

    assert AuthService.verify_mfa_code("user-1", "000000", _caller(user)) is False
    nested.rollback.assert_not_called()


def test_accepted_code_does_not_count(monkeypatch):
    monkeypatch.setattr(AuthService, "_accept_totp", staticmethod(lambda *a, **k: True))
    nested = _nested()
    monkeypatch.setattr(AuthService, "_lock_savepoint", staticmethod(lambda s: nested))

    def boom():
        raise AssertionError("a valid code must not count as a failure")

    monkeypatch.setattr(uow_module, "get_db_session", boom)

    assert AuthService.verify_mfa_code("user-1", "123456", _caller(_user())) is True
    nested.rollback.assert_not_called()


def test_login_discards_the_pending_reset_before_the_code_is_checked(monkeypatch):
    """begin_nested flushes before SAVEPOINT. The reset must not be pending then."""
    user = _user(mfa_enabled=True, failed_login_count=0, login_count=4)
    seen = {}

    class _TrackingSession:
        def __contains__(self, obj):
            return True

        def expire(self, obj):
            seen["expired_before_verify"] = True
            # The committed row still has the failures. The in-memory 0 is gone.
            obj.failed_login_count = 4
            obj.login_count = 3

    def verify(user_id, code, session):
        seen["count_during_verify"] = user.failed_login_count
        return False

    monkeypatch.setattr(
        auth.AuthService, "authenticate_user", staticmethod(lambda *a, **k: user)
    )
    monkeypatch.setattr(auth.AuthService, "verify_mfa_code", staticmethod(verify))
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[request_unit_of_work] = lambda: _TrackingSession()
    limiter.reset()
    client = TestClient(app)

    resp = client.post(
        "/api/auth/login",
        json={"username_or_email": "victim", "password": "pw", "mfa_code": "000000"},
    )

    assert resp.status_code == 401
    assert seen == {"expired_before_verify": True, "count_during_verify": 4}


def test_accepted_login_code_restores_the_reset(monkeypatch):
    user = _user(mfa_enabled=True, failed_login_count=0, login_count=4)

    class _TrackingSession:
        def __contains__(self, obj):
            return True

        def expire(self, obj):
            obj.failed_login_count = 4
            obj.locked_until = utcnow()
            obj.login_count = 3

        def add(self, obj):
            pass

    monkeypatch.setattr(
        auth.AuthService, "authenticate_user", staticmethod(lambda *a, **k: user)
    )
    monkeypatch.setattr(
        auth.AuthService, "verify_mfa_code", staticmethod(lambda *a, **k: True)
    )
    monkeypatch.setattr(
        auth.AuthService, "generate_jwt_token", staticmethod(lambda *a, **k: "tok")
    )
    monkeypatch.setattr(
        auth.AuthService, "verify_jwt_token", staticmethod(lambda t: {})
    )
    monkeypatch.setattr(auth, "_user_payload", lambda u, s: {})
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[request_unit_of_work] = lambda: _TrackingSession()
    limiter.reset()
    client = TestClient(app)

    resp = client.post(
        "/api/auth/login",
        json={"username_or_email": "victim", "password": "pw", "mfa_code": "123456"},
    )

    assert resp.status_code == 200
    assert user.failed_login_count == 0
    assert user.locked_until is None
    assert user.login_count == 4


def test_wrong_enable_code_counts(monkeypatch):
    monkeypatch.setattr(auth_module, "LOCKOUT_THRESHOLD", 5)
    monkeypatch.setattr(
        AuthService, "_accept_totp", staticmethod(lambda *a, **k: False)
    )
    nested = _nested()
    monkeypatch.setattr(AuthService, "_lock_savepoint", staticmethod(lambda s: nested))
    own_user = _user(failed_login_count=0)
    own = _own(monkeypatch, own_user)

    assert AuthService.enable_mfa("user-1", "000000", _caller(_user())) is None

    assert own_user.failed_login_count == 1
    own.commit.assert_called_once()
