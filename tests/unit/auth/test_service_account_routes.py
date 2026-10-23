"""A service account logs in and refreshes through the API, and nothing else (D2-17b).

Medic's gateway signs in as a Viewer service account. A leaked password for it
must not become a console session, so:

- login and refresh never hand a service account auth cookies, and refuse it
  outright when the request carries a browser marker (``Origin`` or any
  ``Sec-Fetch-Site``), with the same reply a wrong password or a bad refresh
  token gets;
- every other ``/api/auth/*`` route refuses a service account's token;
- password reset treats a service account's email or token as unknown.

The requests go through the real app, real tokens and a SQLite user table; only
Redis and mail are swapped.
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.auth import current_user as current_user_module
from core.auth import password_reset
from core.auth.auth_service import AuthService
from core.routing import request_unit_of_work
from core.storage.models import Principal, Role, SignIn, User
from core.storage.models.base import Base
from services.api import main as backend_main
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth

pytestmark = pytest.mark.unit

PASSWORD = "lantern-quarry-velvet-52"
CSRF = "csrf-pair-for-tests"
AUTH_COOKIES = {"access_token", "refresh_token", "csrf_token"}
LOGIN = ("POST", "/api/auth/login")
REFRESH = ("POST", "/api/auth/refresh")


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture(scope="module")
def password_hash():
    return AuthService.hash_password(PASSWORD)  # bcrypt is slow: once per module


@pytest.fixture
def maker(password_hash):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine,
        tables=[
            Role.__table__,
            Principal.__table__,
            User.__table__,
            SignIn.__table__,
        ],
    )
    maker = sessionmaker(bind=engine)
    with maker() as s:
        s.add(
            Role(
                role_id="role-viewer",
                name="viewer",
                description="",
                permissions={"findings.read": True},
            )
        )
        for user_id, username, service in (
            ("u-sa", "medic-a1b2c3d4e5f6", True),
            ("u-viewer", "viewer", False),
        ):
            s.add(
                Principal(
                    principal_id=user_id,
                    kind="service_account" if service else "person",
                    label=username,
                )
            )
            s.add(
                User(
                    user_id=user_id,
                    principal_id=user_id,
                    username=username,
                    email=f"{username}@example.com",
                    password_hash=password_hash,
                    full_name=username,
                    role_id="role-viewer",
                    is_active=True,
                    service_account=service,
                )
            )
        s.commit()
    return maker


@pytest.fixture
def mail(monkeypatch):
    sent = MagicMock()
    monkeypatch.setattr(auth, "send_email", sent)
    return sent


@pytest.fixture
def app(maker, mail, monkeypatch):
    def _session():
        with maker() as s:
            yield s

    async def _not_revoked(payload):
        return False

    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)
    monkeypatch.setattr(current_user_module, "DEV_MODE", False)
    monkeypatch.setattr(current_user_module, "is_token_revoked", _not_revoked)
    monkeypatch.setattr(auth, "is_token_revoked", _not_revoked)
    monkeypatch.setattr(auth, "consume_refresh_jti", AsyncMock(return_value=True))
    monkeypatch.setattr(auth, "blacklist_jti", AsyncMock())
    monkeypatch.setattr(auth, "revoke_all_for_user", AsyncMock())
    monkeypatch.setattr(password_reset, "_redis_client", lambda: redis)
    monkeypatch.setattr("core.storage.unit_of_work.get_db_session", maker)
    backend_main.app.dependency_overrides[request_unit_of_work] = _session
    limiter.reset()
    try:
        yield backend_main.app
    finally:
        backend_main.app.dependency_overrides.pop(request_unit_of_work, None)
        limiter.reset()


def call(app, method, path, *, headers=None, cookies=None, json=None):
    """One request from a fresh client that sends the gateway's CSRF pair, so
    the middleware neither refuses it nor seeds a csrf_token cookie."""
    client = TestClient(
        app,
        raise_server_exceptions=False,
        cookies={"csrf_token": CSRF, **(cookies or {})},
    )
    sent = {"X-CSRF-Token": CSRF, **(headers or {})}
    return client.request(method, path, headers=sent, json=json)


def _token(maker, user_id: str, kind: str = "access") -> str:
    with maker() as s:
        return AuthService.generate_jwt_token(s.get(User, user_id), kind)


def _bearer(maker, user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(maker, user_id)}"}


def _set_cookies(resp) -> dict[str, str]:
    """``{name: raw Set-Cookie line}`` for every cookie the response sets."""
    return {line.split("=", 1)[0]: line for line in resp.headers.get_list("set-cookie")}


def _cleared(resp) -> set[str]:
    """Cookies the response deletes (Starlette's delete_cookie sends Max-Age=0)."""
    return {
        name
        for name, line in _set_cookies(resp).items()
        if re.search(r"max-age=0\b", line, re.I)
    }


def _row(maker, user_id: str) -> tuple:
    with maker() as s:
        u = s.get(User, user_id)
        return u.last_login, u.login_count, u.failed_login_count, u.locked_until


def _login(app, username, password=PASSWORD, headers=None):
    body = {"username_or_email": username, "password": password}
    return call(app, *LOGIN, json=body, headers=headers)


# ---- every other /api/auth route ----------------------------------------------------


def _auth_routes() -> list[tuple[str, str]]:
    """``(method, path)`` for every /api/auth route the app actually mounts."""
    found = []

    def visit(obj) -> None:
        if type(obj).__name__ == "_IncludedRouter":  # FastAPI >= 0.137
            for candidate in obj.effective_candidates():
                visit(candidate)
            return
        path = getattr(obj, "path", None)
        methods = getattr(obj, "methods", None)
        if isinstance(path, str) and path.startswith("/api/auth") and methods:
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                found.append((method, path))

    for route in backend_main.app.routes:
        visit(route)
    return found


def _filled(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _refused(resp) -> bool:
    return (
        resp.status_code == 403
        and resp.json().get("detail") == auth.SERVICE_ACCOUNT_DETAIL
    )


def test_only_login_and_refresh_accept_a_service_account_token(app, maker):
    routes = _auth_routes()
    assert len(routes) >= 14, routes  # the walk reaches the router

    exempt = set()
    for method, path in routes:
        limiter.reset()
        resp = call(app, method, _filled(path), headers=_bearer(maker, "u-sa"))
        if not _refused(resp):
            exempt.add((method, path))

    assert exempt == {LOGIN, REFRESH}


def test_a_normal_viewer_token_is_not_refused_as_a_service_account(app, maker):
    for method, path in _auth_routes():
        limiter.reset()
        resp = call(app, method, _filled(path), headers=_bearer(maker, "u-viewer"))
        assert not _refused(resp), (method, path, resp.text[:200])


def test_a_service_account_access_cookie_is_refused(app, maker):
    resp = call(
        app, "GET", "/api/auth/me", cookies={"access_token": _token(maker, "u-sa")}
    )
    assert _refused(resp)


def test_no_token_still_reaches_the_public_routes(app):
    assert call(app, "GET", "/api/auth/bootstrap").status_code == 200
    assert call(app, "GET", "/api/auth/me").status_code == 401


# ---- login --------------------------------------------------------------------------


def test_gateway_login_gets_body_tokens_and_no_cookies(app):
    resp = _login(app, "medic-a1b2c3d4e5f6")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]
    assert not AUTH_COOKIES & set(_set_cookies(resp))


def test_a_normal_viewer_still_gets_cookies(app):
    resp = _login(app, "viewer", headers={"Origin": "http://testserver"})

    assert resp.status_code == 200
    assert {"access_token", "refresh_token"} <= set(_set_cookies(resp))


def _volatile(a, b) -> set[str]:
    return {
        k
        for k in set(a.headers) | set(b.headers)
        if a.headers.get(k) != b.headers.get(k)
    }


@pytest.mark.parametrize(
    "marker",
    [{"Origin": "http://testserver"}, {"Sec-Fetch-Site": "same-origin"}],
    ids=["origin", "sec-fetch-site"],
)
def test_console_login_by_a_service_account_looks_like_a_wrong_password(
    app, maker, marker, caplog
):
    before = _row(maker, "u-sa")

    with caplog.at_level(logging.WARNING, logger=auth.logger.name):
        browser = _login(app, "medic-a1b2c3d4e5f6", headers=marker)

    assert _row(maker, "u-sa") == before, "not recorded as a login"
    warnings = [
        r
        for r in caplog.records
        if r.name == auth.logger.name and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "medic-a1b2c3d4e5f6" in warnings[0].getMessage()

    wrong = _login(app, "medic-a1b2c3d4e5f6", "not-the-password", headers=marker)
    again = _login(app, "medic-a1b2c3d4e5f6", "still-not-it", headers=marker)
    noise = _volatile(wrong, again)

    assert browser.status_code == wrong.status_code == 401
    assert browser.json() == wrong.json()
    assert browser.headers["www-authenticate"] == "Bearer"
    assert {k: v for k, v in browser.headers.items() if k not in noise} == {
        k: v for k, v in wrong.headers.items() if k not in noise
    }
    assert not AUTH_COOKIES & set(_set_cookies(browser))


# ---- refresh ------------------------------------------------------------------------


def _refresh(app, *, body_token=None, cookie_token=None, headers=None):
    return call(
        app,
        *REFRESH,
        json={"refresh_token": body_token} if body_token else None,
        cookies={"refresh_token": cookie_token} if cookie_token else None,
        headers=headers,
    )


def test_gateway_refresh_with_a_body_token_works_without_cookies(app, maker):
    resp = _refresh(app, body_token=_token(maker, "u-sa", "refresh"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["refresh_token"]
    assert not AUTH_COOKIES & set(_set_cookies(resp))


def _invalid_refresh(app):
    return _refresh(app, body_token="not-a-jwt")


@pytest.mark.parametrize(
    "marker",
    [{"Origin": "http://testserver"}, {"Sec-Fetch-Site": "same-origin"}],
    ids=["origin", "sec-fetch-site"],
)
def test_console_refresh_by_a_service_account_is_an_invalid_token(app, maker, marker):
    resp = _refresh(app, body_token=_token(maker, "u-sa", "refresh"), headers=marker)
    invalid = _invalid_refresh(app)

    assert resp.status_code == invalid.status_code == 401
    assert resp.json() == invalid.json()
    assert {"access_token", "refresh_token"} <= _cleared(resp)


def test_a_service_account_refresh_cookie_is_refused_and_cleared(app, maker):
    resp = _refresh(app, cookie_token=_token(maker, "u-sa", "refresh"))
    invalid = _invalid_refresh(app)

    assert resp.status_code == invalid.status_code == 401
    assert resp.json() == invalid.json()
    assert {"access_token", "refresh_token"} <= _cleared(resp)


def test_a_normal_viewer_refresh_cookie_still_works(app, maker):
    resp = _refresh(app, cookie_token=_token(maker, "u-viewer", "refresh"))

    assert resp.status_code == 200
    assert {"access_token", "refresh_token"} <= set(_set_cookies(resp))


# ---- the flag is read as exactly True -----------------------------------------------


def _stand_in(flag):
    return SimpleNamespace(
        user_id="u-viewer",
        username="viewer",
        email="viewer@example.com",
        role_id="role-viewer",
        is_active=True,
        mfa_enabled=False,
        service_account=flag,
        principal_id="u-viewer",
    )


@pytest.mark.parametrize("flag,refused", [(1, False), ("true", False), (True, True)])
def test_only_an_exact_true_flag_refuses_a_token(
    app, maker, monkeypatch, flag, refused
):
    monkeypatch.setattr(auth, "_get_user", lambda session, user_id: _stand_in(flag))

    resp = call(app, "GET", "/api/auth/me", headers=_bearer(maker, "u-viewer"))

    assert _refused(resp) is refused


@pytest.mark.parametrize("flag", [1, "true"])
def test_a_truthy_flag_is_a_normal_user_at_console_login(app, monkeypatch, flag):
    monkeypatch.setattr(
        auth.AuthService,
        "authenticate_user",
        staticmethod(lambda *a, **k: _stand_in(flag)),
    )
    monkeypatch.setattr(auth, "_user_payload", lambda user, session: {})

    resp = _login(app, "viewer", headers={"Origin": "http://testserver"})

    assert resp.status_code == 200
    assert {"access_token", "refresh_token"} <= set(_set_cookies(resp))


# ---- password reset -----------------------------------------------------------------


def _reset_request(app, email):
    limiter.reset()
    return call(app, "POST", "/api/auth/password-reset/request", json={"email": email})


def test_reset_request_for_a_service_account_is_an_unknown_email(app, mail):
    unknown = _reset_request(app, "nobody@example.com")
    resp = _reset_request(app, "medic-a1b2c3d4e5f6@example.com")

    assert resp.status_code == unknown.status_code == 200
    assert resp.json() == unknown.json()
    mail.assert_not_called()

    _reset_request(app, "viewer@example.com")
    mail.assert_called_once()  # the sanity check: a person still gets mail


def test_reset_confirm_with_a_service_account_token_is_an_invalid_token(app, maker):
    before = _row(maker, "u-sa")
    with maker() as s:
        hash_before = s.get(User, "u-sa").password_hash

    def confirm(token):
        limiter.reset()
        return call(
            app,
            "POST",
            "/api/auth/password-reset/confirm",
            json={"token": token, "new_password": "orchid-tundra-saddle-907"},
        )

    resp = confirm(password_reset.generate_reset_token("u-sa"))
    invalid = confirm("not-a-reset-token")

    assert resp.status_code == invalid.status_code == 400
    assert resp.json() == invalid.json()
    with maker() as s:
        assert s.get(User, "u-sa").password_hash == hash_before
    assert _row(maker, "u-sa") == before
