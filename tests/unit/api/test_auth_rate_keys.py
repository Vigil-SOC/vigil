"""Auth rate limits are per account, and a proxy address is the client only
when that proxy is configured. A shared peer must not put every user in one
bucket, and an untrusted peer must not pick its bucket with a header.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core.auth.auth_service import AuthService
from services.api.middleware import client_address
from services.api.middleware.auth_rate_key import (
    login_rate_key,
    refresh_rate_key,
    reset_confirm_rate_key,
)
from services.api.middleware.client_address import client_host

pytestmark = pytest.mark.unit


def _request(peer, forwarded=None, body=None, cookie=None):
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 40000),
        "server": ("test", 80),
    }
    request = Request(scope)
    if body is not None:
        request._body = body
    return request


@pytest.fixture(autouse=True)
def _proxies(monkeypatch):
    monkeypatch.setattr(
        client_address,
        "get_settings",
        lambda: SimpleNamespace(vigil_trusted_proxies="10.0.0.0/8"),
    )
    client_address._compile.cache_clear()


def test_untrusted_peer_keeps_its_own_address_and_ignores_the_header():
    request = _request("203.0.113.9", forwarded="1.2.3.4")
    assert client_host(request) == "203.0.113.9"


def test_trusted_proxy_uses_the_nearest_untrusted_hop():
    # The client-supplied hop is on the left. The proxy appended the real one.
    request = _request("10.0.0.9", forwarded="8.8.8.8, 203.0.113.5")
    assert client_host(request) == "203.0.113.5"


def test_star_does_not_trust_every_peer(monkeypatch):
    monkeypatch.setattr(
        client_address,
        "get_settings",
        lambda: SimpleNamespace(vigil_trusted_proxies="*"),
    )
    client_address._compile.cache_clear()
    request = _request("203.0.113.9", forwarded="1.2.3.4")
    assert client_host(request) == "203.0.113.9"


def test_login_key_is_the_account_and_not_the_password():
    body = b'{"username_or_email":"Alice","password":"s3cret"}'
    key = login_rate_key(_request("10.0.0.9", body=body))
    assert key == "login:alice"
    assert "s3cret" not in key


def test_login_budgets_are_per_account_behind_one_peer():
    limiter = Limiter(key_func=login_rate_key, strategy="fixed-window")
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    class Body(BaseModel):
        username_or_email: str
        password: str

    @app.post("/login")
    @limiter.limit("2/minute")
    def login(request: Request, payload: Body):
        return {"user": payload.username_or_email}

    client = TestClient(app)

    def post(name):
        return client.post(
            "/login", json={"username_or_email": name, "password": "x"}
        ).status_code

    assert [post("nobody"), post("nobody"), post("nobody")] == [200, 200, 429]
    # Same peer. A different account still has its own budget, and case does not.
    assert [post("Alice"), post("Alice"), post("alice")] == [200, 200, 429]


def test_refresh_key_is_the_token_subject():
    user = SimpleNamespace(
        user_id="user-7", username="ada", email="ada@example.com", role_id="r"
    )
    token = AuthService.generate_jwt_token(user, "refresh")
    keyed = refresh_rate_key(
        _request("10.0.0.9", cookie=f"refresh_token={token}")
    )
    assert keyed == "refresh:user-7"
    assert token not in keyed
    missing = refresh_rate_key(_request("203.0.113.4"))
    assert missing.startswith("refresh:missing:")
    assert missing != keyed


def test_reset_confirm_key_does_not_contain_the_token():
    body = b'{"token":"raw-reset-token","new_password":"hunter2-hunter2"}'
    key = reset_confirm_rate_key(_request("203.0.113.4", body=body))
    assert "raw-reset-token" not in key
    assert "hunter2" not in key
    assert key.startswith("reset-confirm:")
