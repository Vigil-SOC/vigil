"""Only an access token is a session; a refresh token is for /api/auth/refresh alone.

Drives the real ``get_current_user`` (never overridden) through the real app,
substituting only the database session and the Redis revocation lookup.
"""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.auth import current_user as current_user_module
from core.auth.auth_cookies import ACCESS_COOKIE_NAME
from core.auth.auth_service import JWT_ALGORITHM, JWT_SECRET_KEY, AuthService
from core.routing import request_unit_of_work
from core.storage.models import Role, User
from core.storage.models.base import Base
from core.time import utcnow
from services.api import main as backend_main

pytestmark = pytest.mark.unit

ME = "/api/auth/me"
REFRESH = "/api/auth/refresh"


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture
def user():
    return User(
        user_id="u-1",
        username="nestor",
        email="nestor@example.com",
        password_hash="x",
        role_id="r-analyst",
        is_active=True,
    )


@pytest.fixture
def client(monkeypatch, user):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=[Role.__table__, User.__table__])
    maker = sessionmaker(bind=engine)
    with maker() as s:
        s.add(Role(role_id="r-analyst", name="analyst", description="", permissions={}))
        s.add(
            User(
                user_id=user.user_id,
                username=user.username,
                email=user.email,
                password_hash="x",
                full_name="Nestor",
                role_id=user.role_id,
                is_active=True,
            )
        )
        s.commit()

    def _session():
        with maker() as s:
            yield s

    async def _not_revoked(payload):
        return False

    monkeypatch.setattr(current_user_module, "DEV_MODE", False)
    monkeypatch.setattr(current_user_module, "is_token_revoked", _not_revoked)
    monkeypatch.setattr("services.api.routers.auth.is_token_revoked", _not_revoked)
    backend_main.app.dependency_overrides[request_unit_of_work] = _session
    try:
        with TestClient(backend_main.app) as c:
            yield c
    finally:
        backend_main.app.dependency_overrides.pop(request_unit_of_work, None)


def _forged(user, **claims):
    """A correctly signed token whose ``token_type`` is whatever the test says."""
    now = utcnow()
    payload = {
        "user_id": user.user_id,
        "username": user.username,
        "jti": "forged",
        "iat": now,
        "exp": now.replace(year=now.year + 1),
        **claims,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def _tokens(user):
    return {
        "access": AuthService.generate_jwt_token(user, "access"),
        "refresh": AuthService.generate_jwt_token(user, "refresh"),
        "missing": _forged(user),
        "bogus": _forged(user, token_type="bogus"),
    }


def _present(client, token, how):
    if how == "bearer":
        return client.get(ME, headers={"Authorization": f"Bearer {token}"})
    client.cookies.set(ACCESS_COOKIE_NAME, token)
    try:
        return client.get(ME)
    finally:
        client.cookies.clear()


@pytest.mark.parametrize("how", ["bearer", "cookie"])
@pytest.mark.parametrize(
    "kind,expected",
    [("access", 200), ("refresh", 401), ("missing", 401), ("bogus", 401)],
)
def test_only_an_access_token_authenticates(client, user, how, kind, expected):
    response = _present(client, _tokens(user)[kind], how)

    assert response.status_code == expected, response.text
    if expected == 401:
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_refresh_accepts_a_refresh_token_and_rejects_an_access_token(client, user):
    tokens = _tokens(user)

    ok = client.post(REFRESH, json={"refresh_token": tokens["refresh"]})
    assert ok.status_code == 200, ok.text

    # The access token a refresh hands back is itself usable as a session.
    assert _present(client, ok.json()["access_token"], "bearer").status_code == 200

    rejected = client.post(REFRESH, json={"refresh_token": tokens["access"]})
    assert rejected.status_code == 401
