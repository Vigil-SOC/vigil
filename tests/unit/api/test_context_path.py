"""VIGIL_CONTEXT_PATH must prefix cookies, CSRF exemptions, and mounted routes.

Importing ``services.api.main`` with a non-empty context path would bake
``_CONTEXT_PATH`` into this process and hide every ``/api/`` route from
``tests/security/test_route_auth_coverage.py``. These tests never set the
env var for that module: cookies/CSRF read settings at call time, and
router prefix is exercised through ``mount_routers``.
"""

from __future__ import annotations

import os
import re

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

pytestmark = pytest.mark.unit


def _set_cookie_headers(headers) -> list[str]:
    getter = getattr(headers, "get_list", None) or getattr(headers, "getlist")
    return getter("set-cookie")


def _cookie_paths(response: Response) -> dict[str, str]:
    found: dict[str, str] = {}
    for header in _set_cookie_headers(response.headers):
        name = header.split("=", 1)[0]
        match = re.search(r";\s*Path=([^;]*)", header, re.I)
        if match:
            found[name] = match.group(1)
    return found


def _mounted_paths(app: FastAPI) -> list[str]:
    paths: list[str] = []

    def visit(obj) -> None:
        if type(obj).__name__ == "_IncludedRouter":
            for candidate in obj.effective_candidates():
                visit(candidate)
            return
        path = getattr(obj, "path", None)
        if isinstance(path, str):
            paths.append(getattr(obj, "path_format", None) or path)

    for route in app.routes:
        visit(route)
    return paths


@pytest.mark.parametrize(
    "prefix, access, refresh",
    [
        ("", "/", "/api/auth/refresh"),
        ("/", "/", "/api/auth/refresh"),
        ("/vigil", "/vigil/", "/vigil/api/auth/refresh"),
        ("/vigil/", "/vigil/", "/vigil/api/auth/refresh"),
    ],
)
def test_auth_cookie_paths_follow_context_path(monkeypatch, prefix, access, refresh):
    from core.auth.auth_cookies import (
        ACCESS_COOKIE_NAME,
        REFRESH_COOKIE_NAME,
        clear_auth_cookies,
        set_auth_cookies,
    )
    from core.config import get_settings

    monkeypatch.setenv("VIGIL_CONTEXT_PATH", prefix)
    get_settings.cache_clear()

    set_response = Response()
    set_auth_cookies(set_response, "access-token", "refresh-token")
    assert _cookie_paths(set_response) == {
        ACCESS_COOKIE_NAME: access,
        REFRESH_COOKIE_NAME: refresh,
    }

    clear_response = Response()
    clear_auth_cookies(clear_response)
    assert _cookie_paths(clear_response) == {
        ACCESS_COOKIE_NAME: access,
        REFRESH_COOKIE_NAME: refresh,
    }


@pytest.mark.parametrize(
    "raw, context_path, expected",
    [
        (None, "", ("/api/webhooks/", "/api/ingest/", "/mcp")),
        (
            None,
            "/vigil",
            ("/vigil/api/webhooks/", "/vigil/api/ingest/", "/vigil/mcp"),
        ),
        # What env.example and the Helm values actually ship. It names two of
        # the always-exempt paths and not the third, and the third survives:
        # a list an operator sets is added to that set, not put in place of it.
        (
            "/api/webhooks/,/api/ingest/",
            "/vigil",
            ("/vigil/api/webhooks/", "/vigil/api/ingest/", "/vigil/mcp"),
        ),
        # Written fully qualified, so it is left alone -- and it is the same
        # path as the one the always-exempt list contributes, once that one has
        # been qualified too, so it appears once rather than twice.
        (
            "/vigil/api/webhooks/",
            "/vigil",
            ("/vigil/api/webhooks/", "/vigil/api/ingest/", "/vigil/mcp"),
        ),
        (
            "/api/webhooks/",
            "/api",
            ("/api/api/webhooks/", "/api/api/ingest/", "/api/mcp"),
        ),
        # An operator adding one of their own keeps everything that was exempt
        # before it.
        (
            "/api/partner-callback/",
            "",
            ("/api/webhooks/", "/api/ingest/", "/mcp", "/api/partner-callback/"),
        ),
    ],
)
def test_csrf_exempt_paths_follow_context_path(raw, context_path, expected):
    from services.api.middleware.csrf import _parse_exempt_paths

    assert _parse_exempt_paths(raw, context_path) == expected


def test_csrf_middleware_prefixes_helm_defaults_from_settings(monkeypatch):
    """Helm leaves VIGIL_CSRF_EXEMPT_PATHS app-root relative; only the
    context path is set. That is production ``add_middleware(CSRFMiddleware)``."""
    from core.config import get_settings
    from services.api.middleware.csrf import CSRFMiddleware

    monkeypatch.setenv("VIGIL_CONTEXT_PATH", "/vigil")
    monkeypatch.setenv("VIGIL_CSRF_EXEMPT_PATHS", "/api/webhooks/,/api/ingest/")
    get_settings.cache_clear()

    middleware = CSRFMiddleware(FastAPI(), enabled=True)
    assert middleware._is_exempt("/vigil/api/webhooks/darktrace")
    assert middleware._is_exempt("/vigil/api/ingest/upload")
    assert not middleware._is_exempt("/api/webhooks/darktrace")
    assert not middleware._is_exempt("/vigil/api/cases")


def test_csrf_exempt_matching_requires_the_prefix():
    from services.api.middleware.csrf import CSRFMiddleware, _parse_exempt_paths

    inner = FastAPI()
    middleware = CSRFMiddleware(
        inner,
        enabled=True,
        exempt_paths=_parse_exempt_paths(None, "/vigil"),
    )
    assert middleware._is_exempt("/vigil/api/webhooks/darktrace")
    assert middleware._is_exempt("/vigil/api/ingest/upload")
    assert not middleware._is_exempt("/api/webhooks/darktrace")
    assert not middleware._is_exempt("/vigil/api/cases")


def test_csrf_cookie_path_follows_context_path(monkeypatch):
    from core.config import get_settings
    from services.api.middleware.csrf import CSRF_COOKIE_NAME, CSRFMiddleware

    monkeypatch.setenv("VIGIL_CONTEXT_PATH", "/vigil")
    get_settings.cache_clear()

    app = FastAPI()
    app.add_middleware(CSRFMiddleware, enabled=True, report_only=False)

    @app.get("/vigil/")
    def root():
        return {"ok": True}

    response = TestClient(app).get("/vigil/")
    cookie = next(
        c
        for c in _set_cookie_headers(response.headers)
        if c.startswith(CSRF_COOKIE_NAME)
    )
    match = re.search(r";\s*Path=([^;]*)", cookie, re.I)
    assert match is not None
    assert match.group(1) == "/vigil/"


def test_mount_routers_prefixes_api_routes():
    from services.api.discovery import mount_routers

    app = FastAPI()
    mount_routers(app, context_path="/vigil")
    paths = _mounted_paths(app)

    assert any(p.startswith("/vigil/api/") for p in paths)
    assert any(p.startswith("/vigil/api/auth") for p in paths)
    assert not any(p.startswith("/api/") and not p.startswith("/vigil/") for p in paths)
    assert not any(p == "/metrics" or p.startswith("/metrics") for p in paths)


def test_mount_routers_empty_prefix_keeps_root_api_paths():
    from services.api.discovery import mount_routers

    app = FastAPI()
    mount_routers(app, context_path="")
    paths = _mounted_paths(app)

    assert any(p.startswith("/api/") for p in paths)
    assert not any(p.startswith("/vigil/") for p in paths)


# The value both shipped configs set: env.example and infra/helm/vigil/values.yaml.
# Asserting against the default instead would assert against the one setting no
# install runs.
_AS_SHIPPED = "/api/webhooks/,/api/ingest/"


def test_the_mcp_surface_is_exempt_from_csrf():
    """It is reached with a credential in a header, by something that is not a
    browser. There is no cookie session for a forged request to borrow, and a
    caller that cannot be handed a csrf_token cookie could never satisfy the
    check -- so enforcing it would refuse every MCP call the day an operator
    turns enforcement on."""
    from services.api.middleware.csrf import CSRFMiddleware, _parse_exempt_paths

    middleware = CSRFMiddleware(
        FastAPI(),
        enabled=True,
        exempt_paths=_parse_exempt_paths(_AS_SHIPPED, "/vigil"),
    )
    assert middleware._is_exempt("/vigil/mcp")
    assert middleware._is_exempt("/vigil/mcp/")
    assert not middleware._is_exempt("/mcp")


def test_the_shipped_configs_do_not_take_the_exemption_away():
    """Every install sets this variable, so a default it replaced would be a
    default nothing runs -- which is how /mcp came to be exempt only on a
    machine nobody had configured."""
    from pathlib import Path

    from services.api.middleware.csrf import _parse_exempt_paths

    root = Path(__file__).resolve().parents[3]
    shipped = [
        line
        for path in (root / "env.example", root / "infra/helm/vigil/values.yaml")
        for line in path.read_text().splitlines()
        if "VIGIL_CSRF_EXEMPT_PATHS" in line and not line.lstrip().startswith("#")
    ]
    assert len(shipped) == 2, "a shipped config stopped naming the variable"

    for line in shipped:
        raw = line.split("=", 1)[1] if "=" in line else line.split(":", 1)[1]
        assert "/mcp" in _parse_exempt_paths(raw.strip().strip('"'), "")
