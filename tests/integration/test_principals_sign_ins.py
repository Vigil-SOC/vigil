"""Principals and sign-ins (#2758), on a database ``python -m core.upgrade``
built from empty, through the real auth and users routes.

Only Redis is replaced. A deleted user keeps its principal and its revoked
credential; a refresh keeps ``sid`` and changes ``jti``; a signed-out or
expired sign-in cannot refresh.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import sessionmaker

from core.agents.custom_agent_service import CustomAgentService
from core.auth import current_user as current_user_module
from core.auth import mcp_credential_service, token_blacklist
from core.auth.auth_service import JWT_ALGORITHM, JWT_SECRET_KEY, AuthService
from core.storage.connection import get_db_manager
from core.storage.models import Principal, SignIn, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow
from core.upgrade import command
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth, users
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]

PASSWORD = "Correct-Horse-Battery-9!"


class FakeRedis:
    """The commands the token blacklist issues. ``broken`` fails every write,
    as an unreachable Redis would at logout."""

    def __init__(self):
        self.data = {}
        self.broken = False

    async def set(self, key, value, ex=None, nx=False):
        if self.broken:
            raise ConnectionError("redis is down")
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def get(self, key):
        return self.data.get(key)

    async def exists(self, key):
        return 1 if key in self.data else 0


@pytest.fixture
def db(tmp_path):
    with _scratch(False) as engine:
        code, report = _run(engine, tmp_path, restore_point="rp-1")
        assert code == command.EXIT_OK, report
        manager = get_db_manager()
        saved = manager._engine, manager._session_factory
        manager._engine, manager._session_factory = engine, sessionmaker(bind=engine)
        try:
            yield sessionmaker(bind=engine)
        finally:
            manager._engine, manager._session_factory = saved


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(token_blacklist, "_get_client", lambda: fake)
    return fake


@pytest.fixture
def client(db, redis, monkeypatch):
    monkeypatch.setattr(current_user_module, "DEV_MODE", False)
    monkeypatch.setattr("core.auth.auth_service._is_dev_mode", lambda: False)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(users.router, prefix="/api/users")
    limiter.reset()
    with TestClient(app) as c:
        yield c


def _user(username: str, role_id: str) -> Optional[str]:
    """The new account's user_id, or None if the name is taken."""
    with unit_of_work() as session:
        user = AuthService.create_user(
            username=username,
            email=f"{username}@example.com",
            password=PASSWORD,
            full_name=username,
            role_id=role_id,
            session=session,
        )
        assert user is None or user.principal_id == user.user_id
        return user.user_id if user else None


def _login(client, username: str) -> dict:
    resp = client.post(
        "/api/auth/login", json={"username_or_email": username, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    client.cookies.clear()  # each call names its tokens explicitly
    return resp.json()


def _refresh(client, refresh_token: str):
    resp = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    client.cookies.clear()
    return resp


def _claims(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])


def test_a_deleted_user_keeps_its_principal_and_revoked_credential(db, client):
    admin = _user("admin1", "role-admin")
    ana = _user("ana", "role-analyst")
    minted = mcp_credential_service.mint(ana, "ana's laptop")
    ana_sid = _claims(_login(client, "ana")["access_token"])["sid"]
    bearer = {"Authorization": f"Bearer {_login(client, 'admin1')['access_token']}"}

    resp = client.delete(f"/api/users/{ana}", headers=bearer)
    assert resp.status_code == 200, resp.text

    with db() as s:
        principal = s.get(Principal, ana)
        assert (principal.kind, principal.label) == ("person", "ana")
        assert principal.deleted_at is not None
        assert s.get(User, ana).is_active is False
        [credential] = mcp_credential_service.list_for_user(
            ana, include_revoked=True, session=s
        )
        assert credential.user_id == ana and credential.revoked_at is not None
        sign_in = s.get(SignIn, ana_sid)
        assert (sign_in.principal_id, sign_in.end_reason) == (ana, "revoked")
        assert s.get(Principal, admin).deleted_at is None

    assert mcp_credential_service.authenticate(minted.token) is None
    assert client.get(f"/api/users/{ana}", headers=bearer).status_code == 404
    listed = client.get("/api/users/", headers=bearer).json()["users"]
    assert ana not in {u["user_id"] for u in listed}
    # The name stays taken, so old records keep pointing at one account.
    assert _user("ana", "role-analyst") is None


def test_refresh_keeps_the_sign_in_and_sign_out_ends_it(db, client, redis):
    _user("ben", "role-analyst")
    tokens = _login(client, "ben")
    first = _claims(tokens["refresh_token"])
    assert _claims(tokens["access_token"])["sid"] == first["sid"]

    resp = _refresh(client, tokens["refresh_token"])
    assert resp.status_code == 200, resp.text
    second = resp.json()
    for token in (second["access_token"], second["refresh_token"]):
        claims = _claims(token)
        assert claims["sid"] == first["sid"]
        assert claims["jti"] != first["jti"]

    # Redis is down at logout, so no jti is blacklisted; the ended sign-in alone
    # refuses the refresh token that was never used.
    redis.broken = True
    resp = client.post(
        "/api/auth/logout",
        json={"refresh_token": second["refresh_token"]},
        headers={"Authorization": f"Bearer {second['access_token']}"},
    )
    assert resp.status_code == 200, resp.text
    redis.broken = False
    with db() as s:
        assert s.get(SignIn, first["sid"]).end_reason == "sign_out"

    resp = _refresh(client, second["refresh_token"])
    assert (resp.status_code, resp.json()["detail"]) == (401, "Sign-in has ended")


def _expired_refresh(user_id: str, sid: str) -> str:
    now = utcnow()
    return jwt.encode(
        {
            "user_id": user_id,
            "token_type": "refresh",
            "jti": "expired",
            "sid": sid,
            "iat": now - timedelta(days=8),
            "exp": now - timedelta(seconds=5),
        },
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )


def test_an_expired_refresh_ends_its_sign_in_only_once_the_latest_has(db, client):
    cy = _user("cy", "role-analyst")
    sid = _claims(_login(client, "cy")["refresh_token"])["sid"]

    # An older, rotated token ran out while the sign-in's latest has not.
    assert _refresh(client, _expired_refresh(cy, sid)).status_code == 401
    with db() as s:
        assert s.get(SignIn, sid).ended_at is None
        s.get(SignIn, sid).expires_at = utcnow() - timedelta(seconds=1)
        s.commit()

    assert _refresh(client, _expired_refresh(cy, sid)).status_code == 401
    with db() as s:
        assert s.get(SignIn, sid).end_reason == "expiry"


def test_a_custom_agent_principal_outlives_the_agent_and_returns_with_its_id(db):
    service = CustomAgentService()
    agent_id = service.create_agent({"name": "Beacon Hunter", "role": "hunt"})["id"]
    principal_id = f"agent:{agent_id}"
    with db() as s:
        assert s.get(Principal, principal_id).kind == "agent"
        # Seeded by the revision: the system, built-ins and plugins.
        assert s.get(Principal, "system").kind == "system"
        assert s.get(Principal, "agent:triage").label == "Triage agent"
        assert s.get(Principal, "agent:chat").label == "the chat assistant"
        assert s.get(Principal, "agent:orchestrator") is None

    assert service.delete_agent(agent_id)
    with db() as s:
        assert s.get(Principal, principal_id).deleted_at is not None

    service.create_agent({"name": "Beacon Hunter", "role": "hunt"})
    with db() as s:
        assert s.get(Principal, principal_id).deleted_at is None
