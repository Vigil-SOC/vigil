"""A wrong MFA code counts toward the same lock as a wrong password, and a
locked account gets 423 from login and from the MFA routes."""

from __future__ import annotations

import os
from datetime import timedelta

import bcrypt
import pyotp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import core.storage.unit_of_work as uow_module
from core.auth import auth_service
from core.auth.auth_service import AuthService
from core.storage.models import Base, Principal, Role, User
from core.time import utcnow
from services.api.errors import register_exception_handlers
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth

pytestmark = [pytest.mark.integration, pytest.mark.database]

SCRATCH_DB = "vigil_test_mfa_failure_lockout"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "0.0.0.0"}  # nosec B104
_CONNECT_ARGS = {"connect_timeout": 5}
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
PASSWORD = "correct-horse-battery"
WRONG_CODE = "000000"


def _url(database: str) -> str:
    user = os.getenv("POSTGRES_USER", "deeptempo")
    password = os.getenv("POSTGRES_PASSWORD", "deeptempo_secure_password_change_me")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"


@pytest.fixture(scope="module")
def engine():
    host = os.getenv("POSTGRES_HOST", "localhost")
    admin = create_engine(
        _url("postgres"), isolation_level="AUTOCOMMIT", connect_args=_CONNECT_ARGS
    )
    try:
        if host not in _LOCAL_HOSTS:
            raise RuntimeError(f"refusing to CREATE/DROP on non-local host {host!r}")
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
            c.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    except Exception as e:  # noqa: BLE001
        admin.dispose()
        if os.getenv("CI"):
            pytest.fail(f"CI must run the MFA lockout tests: {e}")
        pytest.skip(f"requires a local PostgreSQL: {e}")

    scratch = create_engine(
        _url(SCRATCH_DB),
        connect_args={**_CONNECT_ARGS, "options": "-c lock_timeout=5000"},
    )
    with scratch.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        c.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    Base.metadata.create_all(scratch)
    try:
        yield scratch
    finally:
        scratch.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        admin.dispose()


@pytest.fixture
def db(engine, monkeypatch):
    monkeypatch.setattr(uow_module, "get_db_session", lambda: Session(engine))
    with Session(engine) as s:
        s.add(
            Role(role_id="role-viewer", name="Viewer", description="", permissions={})
        )
        s.commit()
    yield engine
    with engine.begin() as c:
        c.execute(text("TRUNCATE users, roles, principals CASCADE"))


def _add_user(engine, **fields) -> None:
    row = dict(
        user_id="u-1",
        principal_id="u-1",
        username="analyst",
        email="analyst@example.com",
        password_hash=bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode(),
        full_name="Analyst",
        role_id="role-viewer",
        mfa_enabled=True,
        mfa_secret=AuthService._encrypt_mfa_secret(TOTP_SECRET),
    )
    row.update(fields)
    with Session(engine) as s:
        s.add(Principal(principal_id="u-1", kind="person"))
        s.flush()
        s.add(User(**row))
        s.commit()


def _stored(engine) -> User:
    with Session(engine, expire_on_commit=False) as s:
        return s.get(User, "u-1")


def _client(current_user: User | None = None) -> TestClient:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    register_exception_handlers(app)
    app.include_router(auth.router, prefix="/api/auth")
    if current_user is not None:
        app.dependency_overrides[get_current_active_user] = lambda: current_user
    limiter.reset()
    return TestClient(app)


def _login(client: TestClient, code: str):
    return client.post(
        "/api/auth/login",
        json={"username_or_email": "analyst", "password": PASSWORD, "mfa_code": code},
    )


def test_wrong_login_code_at_the_threshold_locks_the_account(db):
    _add_user(db, failed_login_count=auth_service.LOCKOUT_THRESHOLD - 1)
    client = _client()

    assert _login(client, WRONG_CODE).status_code == 401

    user = _stored(db)
    assert user.failed_login_count == auth_service.LOCKOUT_THRESHOLD
    assert utcnow() < user.locked_until
    assert user.locked_until <= utcnow() + timedelta(
        minutes=auth_service.LOCKOUT_DURATION_MINUTES
    )

    resp = _login(client, pyotp.TOTP(TOTP_SECRET).now())
    assert resp.status_code == 423
    assert (
        0
        < int(resp.headers["retry-after"])
        <= (auth_service.LOCKOUT_DURATION_MINUTES * 60)
    )


def test_correct_login_code_clears_the_count(db):
    _add_user(db, failed_login_count=3, login_count=4)

    resp = _login(_client(), pyotp.TOTP(TOTP_SECRET).now())

    assert resp.status_code == 200
    user = _stored(db)
    assert user.failed_login_count == 0
    assert user.locked_until is None
    assert user.login_count == 5


def test_wrong_code_on_a_locked_account_is_not_counted_again(db):
    _add_user(db, failed_login_count=5, locked_until=utcnow() + timedelta(minutes=5))

    with Session(db) as s:
        assert AuthService.verify_mfa_code("u-1", WRONG_CODE, s) is False
        s.rollback()

    assert _stored(db).failed_login_count == 5


def test_wrong_enable_code_counts(db):
    _add_user(db, mfa_enabled=False)

    with Session(db) as s:
        assert AuthService.enable_mfa("u-1", WRONG_CODE, s) is None
        s.rollback()

    user = _stored(db)
    assert user.failed_login_count == 1
    assert user.mfa_enabled is False


@pytest.mark.parametrize("method,path", [("POST", "/mfa/verify"), ("DELETE", "/mfa")])
def test_mfa_routes_refuse_a_locked_account_with_423(db, method, path):
    _add_user(db, failed_login_count=5, locked_until=utcnow() + timedelta(minutes=5))
    client = _client(current_user=_stored(db))

    resp = client.request(
        method, f"/api/auth{path}", json={"code": pyotp.TOTP(TOTP_SECRET).now()}
    )

    assert resp.status_code == 423
    assert int(resp.headers["retry-after"]) > 0
    user = _stored(db)
    assert user.failed_login_count == 5
    assert user.mfa_enabled is True
