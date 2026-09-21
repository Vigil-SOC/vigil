"""A clean install can create its first admin, sign in, and reach an
authenticated route (#914).

``POST /api/auth/bootstrap`` and the login screen that calls it were wired long
before anything exercised them: every development environment ran with the
``DEV_MODE`` bypass, so the authenticated first-run had never been proven. This
drives the chain a browser performs against an **empty** database — bootstrap
status, first-admin creation, login, an authenticated call — and checks the two
doors close behind it: bootstrap refuses once an account exists, and the same
route answers 401 without a session.

The database is a scratch one created here (``roles`` seeded, ``users`` empty)
so the test never depends on, or truncates, whatever the configured database
holds. The app's shared ``DatabaseManager`` is retargeted at it for the module
and restored afterwards; the request unit of work and ``AuthService``'s own
sessions all go through that manager, so overriding a single dependency would
not have been enough.

Skips when no local Postgres or Redis answers (token revocation is fail-closed,
so a session cannot be verified without Redis) — except under ``CI``, where a
missing service is a provisioning failure and must fail rather than pass by
skipping.
"""

from __future__ import annotations

import os

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from core import redis_client
from core.config import DEFAULT_REDIS_URL, get_settings
from core.storage.connection import DatabaseConfig, get_db_manager
from core.storage.models import Base, Role
from services.api.middleware.rate_limit import limiter

pytestmark = [pytest.mark.integration, pytest.mark.database]

SCRATCH_DB = "vigil_test_first_admin"

# These fixtures CREATE/DROP DATABASE; only ever against a loopback host.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "0.0.0.0"}
_CONNECT_ARGS = {"connect_timeout": 5}

ADMIN = {
    "username": "first-admin",
    "email": "first-admin@example.com",
    "password": "harbour-lantern-quartz-91",
    "full_name": "First Admin",
}


def _url(database: str) -> str:
    """DSN from the POSTGRES_* variables CI sets, else the compose defaults."""
    user = os.getenv("POSTGRES_USER", "deeptempo")
    password = os.getenv("POSTGRES_PASSWORD", "deeptempo_secure_password_change_me")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def _admin_engine():
    return create_engine(
        _url("postgres"), isolation_level="AUTOCOMMIT", connect_args=_CONNECT_ARGS
    )


def _unavailable_reason():
    host = os.getenv("POSTGRES_HOST", "localhost")
    if host not in _LOCAL_HOSTS:
        return f"refusing to CREATE/DROP a database on non-local POSTGRES_HOST {host!r}"
    try:
        with _admin_engine().connect():
            pass
    except Exception as e:  # noqa: BLE001
        return f"requires a local PostgreSQL (docker compose up -d postgres): {e}"
    try:
        redis.Redis.from_url(
            get_settings().redis_url or DEFAULT_REDIS_URL, socket_connect_timeout=5
        ).ping()
    except Exception as e:  # noqa: BLE001
        return f"requires a local Redis (docker compose up -d redis): {e}"
    return None


@pytest.fixture(scope="module")
def empty_database():
    """A freshly provisioned database with roles and no users, wired into the app."""
    reason = _unavailable_reason()
    if reason:
        if os.getenv("CI"):
            pytest.fail(f"CI must run the bootstrap proof: {reason}")
        pytest.skip(reason)
    # The shared redis.asyncio client binds to the event loop that first uses
    # it; an earlier module's TestClient loop is closed by now, so start fresh.
    redis_client._client = None

    admin = _admin_engine()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))

    scratch = create_engine(_url(SCRATCH_DB), connect_args=_CONNECT_ARGS)
    with scratch.begin() as c:
        # The findings GIN index needs pg_trgm before create_all can build it.
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        c.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    Base.metadata.create_all(scratch)
    # What scripts/seed_reference_data.py provides on a real install: the role
    # the first admin is created with. users stays empty on purpose.
    with Session(scratch) as s:
        s.add(
            Role(
                role_id="role-admin",
                name="Admin",
                description="Full system access",
                permissions={"users.read": True, "users.write": True},
                is_system_role=True,
            )
        )
        s.commit()
    scratch.dispose()

    manager = get_db_manager()
    manager.retarget(DatabaseConfig(connection_string=_url(SCRATCH_DB)))
    # DatabaseConfig falls back to POSTGRES_* on an unparsable DSN; the chain
    # below must never run against the configured database by accident.
    assert manager.config.database == SCRATCH_DB
    try:
        yield
    finally:
        # Back to the configured database before the scratch one disappears, so
        # later tests in the session do not inherit a dead engine.
        manager.retarget(DatabaseConfig(), validate=False)
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture(scope="module")
def client(empty_database):
    from services.api.main import app  # after retarget: startup may touch the DB

    # bootstrap and login are rate-limited per client IP, in Redis when one is
    # configured, so a second run inside a minute would 429 on nothing to do
    # with what this proves.
    was_enabled = limiter.enabled
    limiter.enabled = False
    # https: the auth cookies are Secure, and the cookie jar withholds those
    # over plain http.
    with TestClient(app, base_url="https://testserver") as c:
        yield c
    limiter.enabled = was_enabled


def _csrf(client: TestClient) -> dict:
    # What the SPA does: echo the csrf_token cookie the first response seeded.
    # Sent whether or not CSRF is enabled in this process (other modules turn it
    # off at collection), so the chain holds either way.
    return {"X-CSRF-Token": client.cookies.get("csrf_token", "")}


def test_first_admin_can_bootstrap_login_and_reach_an_authenticated_route(client):
    # An empty instance advertises first-run, and refuses to run open.
    assert client.get("/api/auth/bootstrap").json() == {"required": True}
    assert client.get("/api/auth/me").status_code == 401

    created = client.post("/api/auth/bootstrap", json=ADMIN, headers=_csrf(client))
    assert created.status_code == 201, created.text
    assert created.json()["username"] == ADMIN["username"]
    assert client.get("/api/auth/bootstrap").json() == {"required": False}

    login = client.post(
        "/api/auth/login",
        json={
            "username_or_email": ADMIN["username"],
            "password": ADMIN["password"],
        },
        headers=_csrf(client),
    )
    assert login.status_code == 200, login.text
    body = login.json()
    assert body["user"]["role_id"] == "role-admin"
    assert "users.write" in body["user"]["permissions"]

    # Browser flow: the HttpOnly access_token cookie set by /login.
    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["username"] == ADMIN["username"]

    # API-client flow: the same token as a bearer, with no cookies at all. (Same
    # client rather than a second one: the shared async Redis client used for
    # revocation checks is bound to the first TestClient's event loop.)
    client.cookies.clear()
    bearer = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert bearer.status_code == 200, bearer.text
    assert bearer.json()["username"] == ADMIN["username"]


def test_bootstrap_closes_once_an_account_exists(client):
    client.get("/api/auth/bootstrap")  # re-seed the csrf cookie the last test cleared
    again = client.post(
        "/api/auth/bootstrap",
        json={**ADMIN, "username": "second-admin", "email": "second@example.com"},
        headers=_csrf(client),
    )
    assert again.status_code == 403, again.text
    assert "already exists" in again.json()["detail"]
