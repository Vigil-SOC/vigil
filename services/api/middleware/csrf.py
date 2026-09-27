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
  endpoints using bearer/API-key, the MCP surface using a minted credential,
  the agent layer's /internal endpoints using the shared internal token)
  are always exempt; `VIGIL_CSRF_EXEMPT_PATHS` adds to that set rather than
  replacing it. Any request whose path starts with one of those prefixes
  skips both the cookie check and the cookie seeding.

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

from core.auth.auth_cookies import context_path_prefix, cookie_root_path
from core.config import get_settings

logger = logging.getLogger(__name__)


CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# CSRF defends a browser session driven by a cookie. These are reached with a
# credential in a header instead, by something that is not a browser -- webhooks,
# ingestion, the /mcp surface, and the agent layer's /internal endpoints -- so
# there is no ambient authority for a forged request to borrow, and a caller
# that cannot be handed a csrf_token cookie could not satisfy the check anyway.
#
# Always, rather than by default. An operator's list is added to these, not
# substituted for them: every shipped config already names a list, so a default
# that a list replaces is a default nothing runs. The one thing dropping one of
# these could achieve is refusing every call to it -- there is no protection on
# the other side of the trade, because the check these skip is one their callers
# have no way to pass. /internal/ still runs through _apply_context_path: the
# routers are mounted under VIGIL_CONTEXT_PATH, so the prefixed form is the one
# a sub-path deploy actually serves.
_ALWAYS_EXEMPT = ("/api/webhooks/", "/api/ingest/", "/mcp", "/internal/")


def _apply_context_path(path: str, prefix: str) -> str:
    """Prefix an app-root path with VIGIL_CONTEXT_PATH.

    Already-prefixed paths (``{prefix}/api/...``) are left alone so an
    operator can set fully qualified VIGIL_CSRF_EXEMPT_PATHS without
    doubling. The skip is ``{prefix}/api``, not ``{prefix}/``, so a
    context path of ``/api`` does not treat Helm's ``/api/webhooks/``
    as already done.
    """
    if not path.startswith("/"):
        path = "/" + path
    if not prefix:
        return path
    if path == prefix or path == prefix + "/api" or path.startswith(prefix + "/api/"):
        return path
    return f"{prefix}{path}"


def _parse_exempt_paths(
    raw: Optional[str], context_path: Optional[str] = None
) -> tuple:
    prefix = context_path_prefix() if context_path is None else context_path.rstrip("/")
    configured = tuple(p.strip() for p in raw.split(",") if p.strip()) if raw else ()
    # Deduplicated after the context path is applied, not before: an operator
    # who wrote a path fully qualified names the same prefix as the always-exempt
    # one that gets qualified here, and the two are only equal once both are.
    # dict.fromkeys rather than a set so the order stays the one a reader of the
    # config sees.
    return tuple(
        dict.fromkeys(
            _apply_context_path(p, prefix) for p in _ALWAYS_EXEMPT + configured
        )
    )


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
                path=cookie_root_path(),
            )

        return response
