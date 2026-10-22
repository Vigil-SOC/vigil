"""A credential or claims change that cannot revoke old tokens is a 503, and the
request's unit of work rolls the change back so a retry starts clean."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import token_blacklist as tb
from core.auth.auth_service import AuthService
from services.api.errors import register_exception_handlers
from services.api.middleware.auth import get_current_active_user, get_current_user
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth, users

pytestmark = pytest.mark.unit

CURRENT = "trombone-sunrise-ledger-88"
NEW = "glacier-thimble-orchard-73"


class _Redis:
    def __init__(self, error=None):
        self.error = error
        self.sets = []

    async def set(self, key, value, ex=None):
        if self.error:
            raise self.error
        self.sets.append(key)


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
        password_history=[],
        password_changed_at=None,
        is_verified=True,
        is_active=True,
        role_id="role-analyst",
        mfa_enabled=False,
        login_count=1,
        failed_login_count=3,
        locked_until=None,
    )


@pytest.fixture
def db():
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None
    with patch("core.storage.unit_of_work.get_db_session", return_value=session):
        yield session


@pytest.fixture
def http(user, db):
    app = FastAPI()
    app.state.limiter = limiter
    register_exception_handlers(app)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(users.router, prefix="/api/users")
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_user] = lambda: user
    limiter.reset()
    with (
        patch.object(auth, "verify_reset_token", new=AsyncMock(return_value="u-1")),
        patch.object(auth, "_get_user", return_value=user),
        patch.object(
            users, "_apply_user_update", return_value=(user, "Role changed", {})
        ),
    ):
        yield TestClient(app, raise_server_exceptions=False)


REQUESTS = {
    "change-password": (
        "post",
        "/api/auth/change-password",
        {"current_password": CURRENT, "new_password": NEW},
    ),
    "reset-confirm": (
        "post",
        "/api/auth/password-reset/confirm",
        {"token": "t", "new_password": NEW},
    ),
    "email-change": (
        "put",
        "/api/auth/me",
        {"email": "ada@example.com", "current_password": CURRENT},
    ),
    "admin-role-change": ("put", "/api/users/u-1", {"role_id": "role-viewer"}),
}


@pytest.mark.parametrize("name", REQUESTS)
@pytest.mark.parametrize(
    "client", [None, _Redis(ConnectionError("redis down"))], ids=["no-client", "down"]
)
def test_unrevoked_change_is_503_and_rolled_back(http, db, monkeypatch, name, client):
    monkeypatch.setattr(tb, "_get_client", lambda: client)
    method, path, body = REQUESTS[name]
    resp = getattr(http, method)(path, json=body)
    assert resp.status_code == 503, resp.text
    assert resp.json()["code"] == "REVOCATION_UNAVAILABLE"
    db.rollback.assert_called_once()
    db.commit.assert_not_called()


@pytest.mark.parametrize("name", REQUESTS)
def test_revoked_change_commits(http, db, monkeypatch, name):
    redis = _Redis()
    monkeypatch.setattr(tb, "_get_client", lambda: redis)
    method, path, body = REQUESTS[name]
    resp = getattr(http, method)(path, json=body)
    assert resp.status_code == 200, resp.text
    assert redis.sets == ["user_revoked_before:u-1"]
    db.commit.assert_called_once()
    db.rollback.assert_not_called()
