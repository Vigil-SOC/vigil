"""
Double-submit cookie CSRF middleware.

Disabled by default in this PR (`VIGIL_CSRF_ENABLED=false`). PR 4 flips it
on once the frontend starts injecting the `X-CSRF-Token` header and uses
HttpOnly auth cookies.

How it works:

- On safe methods (GET, HEAD, OPTIONS), ensure the response carries a
  `csrf_token` cookie. The cookie is deliberately **not** HttpOnly — the
  frontend needs to read it with JS and echo it back as `X-CSRF-Token`.
- On unsafe methods (POST, PUT, PATCH, DELETE), require that the incoming
  `X-CSRF-Token` header matches the `csrf_token` cookie. Reject with 403
  otherwise. This is the double-submit pattern: an attacker triggering a
  cross-site request can't read the cookie (same-origin policy), so they
  can't forge a matching header.

Exempt paths:
- Endpoints that authenticate themselves (webhooks using HMAC, ingestion
  endpoints using bearer/API-key) opt out via `VIGIL_CSRF_EXEMPT_PATHS`.
  Any request whose path starts with one of the configured prefixes skips
  both the cookie check and the cookie seeding.

Report-only mode:
- `VIGIL_CSRF_REPORT_ONLY=true` logs violations at WARNING but lets the
  request through. Useful for the rollout window — flip enforcement on
  after a few days of clean logs.
"""

import logging
import secrets
from typing import Callable, Iterable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from core.config import get_settings

logger = logging.getLogger(__name__)


CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_DEFAULT_EXEMPT = ("/api/webhooks/", "/api/ingest/")


def _parse_exempt_paths(raw: Optional[str]) -> tuple:
    if not raw:
        return _DEFAULT_EXEMPT
    return tuple(p.strip() for p in raw.split(",") if p.strip())


class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        enabled: Optional[bool] = None,
        report_only: Optional[bool] = None,
        exempt_paths: Optional[Iterable[str]] = None,
        cookie_secure: Optional[bool] = None,
    ):
        super().__init__(app)
        self.enabled = get_settings().vigil_csrf_enabled if enabled is None else enabled
        self.report_only = (
            get_settings().vigil_csrf_report_only
            if report_only is None
            else report_only
        )
        self.exempt_paths = (
            tuple(exempt_paths)
            if exempt_paths is not None
            else _parse_exempt_paths(get_settings().vigil_csrf_exempt_paths)
        )
        self.cookie_secure = (
            get_settings().vigil_cookie_secure
            if cookie_secure is None
            else cookie_secure
        )

    def _is_exempt(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.exempt_paths)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled:
            return await call_next(request)

        path = request.url.path
        if self._is_exempt(path):
            return await call_next(request)

        if request.method in UNSAFE_METHODS:
            header_token = request.headers.get(CSRF_HEADER_NAME)
            cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
            ok = (
                header_token
                and cookie_token
                and secrets.compare_digest(header_token, cookie_token)
            )
            if not ok:
                logger.warning(
                    "CSRF violation: path=%s method=%s cookie_present=%s header_present=%s report_only=%s",
                    path,
                    request.method,
                    bool(cookie_token),
                    bool(header_token),
                    self.report_only,
                )
                if not self.report_only:
                    return JSONResponse(
                        {"detail": "CSRF token missing or invalid"},
                        status_code=403,
                    )

        response = await call_next(request)

        # Seed the csrf_token cookie on every response when the client
        # doesn't already have one — including rejected POSTs (so the next
        # attempt has something to echo) and error responses like 401
        # (the frontend's loadUser flow gets a cookie from an unauth 401).
        if CSRF_COOKIE_NAME not in request.cookies:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                secrets.token_urlsafe(32),
                httponly=False,  # JS must be able to read it
                secure=self.cookie_secure,
                samesite="strict",
                path="/",
            )

        return response
