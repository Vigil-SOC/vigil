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

from core.auth.auth_cookies import ACCESS_COOKIE_NAME
from core.config import get_settings
from services.api.middleware.csrf import (
    COOKIE_MISSING_DETAIL,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFMiddleware,
    _parse_exempt_paths,
)

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

pytestmark = pytest.mark.unit


def _set_cookie_headers(headers) -> list[str]:
    getter = getattr(headers, "get_list", None) or getattr(headers, "getlist")
    return getter("set-cookie")


def _cookie_paths(response: Response) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Live cookies (name → path) and cookies cleared with Max-Age=0."""
    live: dict[str, str] = {}
    cleared: list[tuple[str, str]] = []
    for header in _set_cookie_headers(response.headers):
        name = header.split("=", 1)[0]
        match = re.search(r";\s*Path=([^;]*)", header, re.I)
        if not match:
            continue
        path = match.group(1)
        if re.search(r"(?:Max-Age=0|expires=Thu, 01 Jan 1970)", header, re.I):
            cleared.append((name, path))
        else:
            live[name] = path
    return live, cleared


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
    "prefix, access, refresh, legacy_refresh",
    [
        ("", "/", "/api/auth/", "/api/auth/refresh"),
        ("/", "/", "/api/auth/", "/api/auth/refresh"),
        ("/vigil", "/vigil/", "/vigil/api/auth/", "/vigil/api/auth/refresh"),
        ("/vigil/", "/vigil/", "/vigil/api/auth/", "/vigil/api/auth/refresh"),
    ],
)
def test_auth_cookie_paths_follow_context_path(
    monkeypatch, prefix, access, refresh, legacy_refresh
):
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
    live, cleared = _cookie_paths(set_response)
    assert live == {
        ACCESS_COOKIE_NAME: access,
        REFRESH_COOKIE_NAME: refresh,
    }
    # Setting the new path does not replace a cookie stored at the old one.
    assert cleared == [(REFRESH_COOKIE_NAME, legacy_refresh)]

    clear_response = Response()
    clear_auth_cookies(clear_response)
    live, cleared = _cookie_paths(clear_response)
    assert live == {}
    assert cleared == [
        (ACCESS_COOKIE_NAME, access),
        (REFRESH_COOKIE_NAME, refresh),
        (REFRESH_COOKIE_NAME, legacy_refresh),
    ]


@pytest.mark.parametrize(
    "raw, context_path, expected",
    [
        (None, "", ("/api/webhooks/", "/mcp", "/internal/")),
        (
            None,
            "/vigil",
            (
                "/vigil/api/webhooks/",
                "/vigil/mcp",
                "/vigil/internal/",
            ),
        ),
        # What env.example and the Helm values actually ship. It names one of
        # the always-exempt paths and not the others, and the others survive:
        # a list an operator sets is added to that set, not put in place of it.
        # Ingestion is not among them.
        (
            "/api/webhooks/",
            "/vigil",
            (
                "/vigil/api/webhooks/",
                "/vigil/mcp",
                "/vigil/internal/",
            ),
        ),
        # Written fully qualified, so it is left alone -- and it is the same
        # path as the one the always-exempt list contributes, once that one has
        # been qualified too, so it appears once rather than twice.
        (
            "/vigil/api/webhooks/",
            "/vigil",
            (
                "/vigil/api/webhooks/",
                "/vigil/mcp",
                "/vigil/internal/",
            ),
        ),
        (
            "/api/webhooks/",
            "/api",
            ("/api/api/webhooks/", "/api/mcp", "/api/internal/"),
        ),
        # An operator adding one of their own keeps everything that was exempt
        # before it. Naming ingestion still exempts it; the shipped configs
        # no longer do.
        (
            "/api/partner-callback/",
            "",
            (
                "/api/webhooks/",
                "/mcp",
                "/internal/",
                "/api/partner-callback/",
            ),
        ),
        (
            "/api/webhooks/,/api/ingest/",
            "",
            (
                "/api/webhooks/",
                "/mcp",
                "/internal/",
                "/api/ingest/",
            ),
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
    monkeypatch.setenv("VIGIL_CSRF_EXEMPT_PATHS", "/api/webhooks/")
    get_settings.cache_clear()

    middleware = CSRFMiddleware(FastAPI(), enabled=True)
    assert middleware._is_exempt("/vigil/api/webhooks/darktrace")
    assert not middleware._is_exempt("/vigil/api/ingest/upload")
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
    assert not middleware._is_exempt("/vigil/api/ingest/upload")
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
_AS_SHIPPED = "/api/webhooks/"


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


def test_the_agent_layer_internal_surface_is_exempt_from_csrf():
    """agent-serve and agent-worker call /internal with the shared token and
    never hold a browser session, so they cannot present a csrf_token cookie.
    Enforcing the check there refuses every tool call and protects nothing."""
    from services.api.middleware.csrf import CSRFMiddleware, _parse_exempt_paths

    at_root = CSRFMiddleware(
        FastAPI(),
        enabled=True,
        exempt_paths=_parse_exempt_paths(_AS_SHIPPED, ""),
    )
    assert at_root._is_exempt("/internal/tools/invoke")
    assert not at_root._is_exempt("/api/cases/")
    assert not at_root._is_exempt("/api/v1/cases")

    under_vigil = CSRFMiddleware(
        FastAPI(),
        enabled=True,
        exempt_paths=_parse_exempt_paths(_AS_SHIPPED, "/vigil"),
    )
    assert under_vigil._is_exempt("/vigil/internal/tools/invoke")
    assert under_vigil._is_exempt("/vigil/internal/runs/abc/terminal")
    assert not under_vigil._is_exempt("/internal/tools/invoke")
    assert not under_vigil._is_exempt("/vigil/api/cases/")
    assert not under_vigil._is_exempt("/api/cases/")


def test_enforcing_csrf_lets_cookieless_internal_through_and_rejects_api():
    from services.api.middleware.csrf import (
        COOKIE_MISSING_DETAIL,
        CSRFMiddleware,
        _parse_exempt_paths,
    )

    app = FastAPI()
    app.add_middleware(
        CSRFMiddleware,
        enabled=True,
        report_only=False,
        exempt_paths=_parse_exempt_paths(_AS_SHIPPED, ""),
    )

    @app.post("/internal/tools/invoke")
    def invoke():
        return {"ok": True}

    @app.post("/api/cases/")
    def create_case():
        return {"ok": True}

    client = TestClient(app)
    internal = client.post("/internal/tools/invoke")
    assert internal.status_code == 200
    assert internal.json() == {"ok": True}

    rejected = client.post("/api/cases/")
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == COOKIE_MISSING_DETAIL


_INGEST_FORM = {"format": "json", "data_type": "finding", "data": "[]"}


def _enforcing_app(*routes: str, context_path: str = "") -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        CSRFMiddleware,
        enabled=True,
        report_only=False,
        exempt_paths=_parse_exempt_paths(_AS_SHIPPED, context_path),
    )
    for path in routes:

        def _ok():
            return {"ok": True}

        app.post(path)(_ok)
    return app


def test_cookie_authenticated_ingestion_requires_csrf():
    """A session cookie on ingestion is the console. It takes the same check
    as every other cookie-authenticated route. A bearer call sends no session
    cookie and is not a browser session, so the check does not apply to it.
    """
    app = _enforcing_app(
        "/api/ingest/ingest-string", "/api/ingest/upload", "/api/cases/"
    )
    form_paths = ("/api/ingest/ingest-string", "/api/ingest/upload")

    for path in form_paths:
        signed_in = TestClient(app)
        signed_in.cookies.set(ACCESS_COOKIE_NAME, "session")
        rejected = signed_in.post(path, data=_INGEST_FORM)
        assert rejected.status_code == 403
        assert rejected.json()["detail"] == COOKIE_MISSING_DETAIL

        # A same-site form sends both cookies and cannot set the header.
        echoed_cookie = TestClient(app)
        echoed_cookie.cookies.set(ACCESS_COOKIE_NAME, "session")
        echoed_cookie.cookies.set(CSRF_COOKIE_NAME, "abc")
        no_header = echoed_cookie.post(path, data=_INGEST_FORM)
        assert no_header.status_code == 403
        assert no_header.json()["detail"] == "CSRF token missing or invalid"

        echoed = TestClient(app)
        echoed.cookies.set(ACCESS_COOKIE_NAME, "session")
        echoed.cookies.set(CSRF_COOKIE_NAME, "abc")
        allowed = echoed.post(
            path, data=_INGEST_FORM, headers={CSRF_HEADER_NAME: "abc"}
        )
        assert allowed.status_code == 200

        bearer = TestClient(app)
        assert bearer.post(path, data=_INGEST_FORM).status_code == 200

    cookieless_case = TestClient(app).post("/api/cases/")
    assert cookieless_case.status_code == 403
    assert cookieless_case.json()["detail"] == COOKIE_MISSING_DETAIL


def test_ingestion_csrf_follows_the_context_path(monkeypatch):
    monkeypatch.setenv("VIGIL_CONTEXT_PATH", "/vigil")
    get_settings.cache_clear()

    app = _enforcing_app("/vigil/api/ingest/ingest-string", context_path="/vigil")

    signed_in = TestClient(app)
    signed_in.cookies.set(ACCESS_COOKIE_NAME, "session")
    rejected = signed_in.post("/vigil/api/ingest/ingest-string", data=_INGEST_FORM)
    assert rejected.status_code == 403

    assert (
        TestClient(app)
        .post("/vigil/api/ingest/ingest-string", data=_INGEST_FORM)
        .status_code
        == 200
    )


def test_csrf_rejection_names_a_dropped_cookie_apart_from_a_mismatch():
    """A cookie the browser dropped (Secure over plain HTTP) has to tell the
    operator about HTTPS, not report a generic CSRF failure."""
    from services.api.middleware.csrf import (
        COOKIE_MISSING_DETAIL,
        CSRF_COOKIE_NAME,
        CSRF_HEADER_NAME,
        CSRFMiddleware,
    )

    app = FastAPI()
    app.add_middleware(CSRFMiddleware, enabled=True, report_only=False, exempt_paths=())

    @app.post("/api/auth/login")
    def login():
        return {"ok": True}

    client = TestClient(app)
    missing = client.post("/api/auth/login", headers={CSRF_HEADER_NAME: "abc"})
    assert missing.status_code == 403
    assert missing.json()["detail"] == COOKIE_MISSING_DETAIL
    assert "HTTPS" in COOKIE_MISSING_DETAIL

    client.cookies.set(CSRF_COOKIE_NAME, "abc")
    mismatched = client.post("/api/auth/login", headers={CSRF_HEADER_NAME: "xyz"})
    assert mismatched.status_code == 403
    assert mismatched.json()["detail"] == "CSRF token missing or invalid"

    matched = client.post("/api/auth/login", headers={CSRF_HEADER_NAME: "abc"})
    assert matched.status_code == 200


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
        parsed = _parse_exempt_paths(raw.strip().strip('"'), "")
        assert "/mcp" in parsed
        assert "/internal/" in parsed
        assert not any(
            p == "/api/ingest/" or p.endswith("/api/ingest/") for p in parsed
        )
