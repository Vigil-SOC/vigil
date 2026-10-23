"""Password reset without a mail backend (#2247): the request reply is honest
about the backend, and an operator can issue a link from the host."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import password_reset
from core.platform import email_service
from core.routing import request_unit_of_work
from services.api.middleware.rate_limit import limiter
from services.api.routers import auth

pytestmark = pytest.mark.unit

_spec = importlib.util.spec_from_file_location(
    "reset_password_script",
    Path(__file__).resolve().parents[3] / "scripts" / "reset_password.py",
)
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

ADMIN = SimpleNamespace(
    user_id="u-admin",
    username="admin",
    email="admin@example.com",
    full_name="Admin",
    role_id="role-admin",
    is_active=True,
    service_account=False,
)


def _session(user):
    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = user
    return session


@pytest.fixture(autouse=True)
def _mock_redis(monkeypatch):
    client = AsyncMock()
    client.set = AsyncMock(return_value=True)
    monkeypatch.setattr(password_reset, "_redis_client", lambda: client)


def _request(user, settings):
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(auth.router, prefix="/api/auth")
    app.dependency_overrides[request_unit_of_work] = lambda: _session(user)
    limiter.reset()
    with patch.object(auth, "get_settings", return_value=settings), patch.object(
        email_service, "get_settings", return_value=settings
    ), patch.object(auth, "send_email") as send:
        resp = TestClient(app).post(
            "/api/auth/password-reset/request", json={"email": "admin@example.com"}
        )
    assert resp.status_code == 200
    return resp.json()["message"], send


def _settings(backend="smtp", dev_mode=False, frontend_url=""):
    return SimpleNamespace(
        vigil_email_backend=backend,
        dev_mode=dev_mode,
        vigil_frontend_url=frontend_url,
        vigil_context_path="/vigil",
    )


@pytest.mark.parametrize(
    "backend,dev_mode,expect",
    [
        ("console", False, "scripts/reset-password.sh"),
        ("console", True, "server log"),
        ("smtp", False, "a reset link has been sent"),
    ],
)
def test_reply_names_the_backend_not_the_account(backend, dev_mode, expect):
    settings = _settings(backend, dev_mode)
    known, _ = _request(ADMIN, settings)
    unknown, _ = _request(None, settings)
    assert expect in known
    assert known == unknown


@pytest.mark.parametrize(
    "frontend_url,origin",
    [
        ("", ("http", "localhost")),
        ("https://soc.example/vigil", ("https", "soc.example")),
    ],
)
def test_mail_links_to_the_reset_screen(frontend_url, origin):
    _, send = _request(ADMIN, _settings(frontend_url=frontend_url))
    body = send.call_args.kwargs["body"]
    url = urlparse(next(w for w in body.split() if w.startswith("http")))
    assert (url.scheme, url.hostname) == origin
    assert url.path == "/vigil/reset-password"
    token = parse_qs(url.query)["token"][0]
    assert password_reset.decode_reset_token(token) == "u-admin"


def test_list_shows_every_account(capsys):
    inactive = SimpleNamespace(**{**vars(ADMIN), "username": "old", "is_active": False})
    session = MagicMock()
    session.query.return_value.order_by.return_value.all.return_value = [
        ADMIN,
        inactive,
    ]
    assert script.list_users(session) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["admin", "admin@example.com", "role-admin", "active"]
    assert lines[1].split()[-1] == "inactive"


def test_link_carries_a_token_for_that_user(capsys):
    assert (
        script.issue_link(_session(ADMIN), "admin", "https://soc.example/vigil/") == 0
    )
    url = next(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("http")
    )
    parsed = urlparse(url)
    assert parsed.path == "/vigil/reset-password"
    token = parse_qs(parsed.query)["token"][0]
    assert password_reset.decode_reset_token(token) == "u-admin"


@pytest.mark.parametrize(
    "user", [None, SimpleNamespace(**{**vars(ADMIN), "is_active": False})]
)
def test_unknown_or_inactive_user_is_refused(user, capsys):
    assert script.issue_link(_session(user), "admin", "http://x") == 1
    assert "http" not in capsys.readouterr().out
