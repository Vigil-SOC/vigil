"""
Security headers middleware.

Adds HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, and a
Content-Security-Policy to every response. Each header is individually
togglable via env so deployments fronted by a reverse proxy that already
sets these can disable the in-app version and avoid duplicates.

HSTS is only emitted on HTTPS requests (best-effort detection via
request.url.scheme and the X-Forwarded-Proto header) because browsers
ignore HSTS on plain HTTP anyway and emitting it would be noise.
"""

import logging
import os
from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


# A connector origin needs script-src (its bundle is a module script) + connect-src
# (its BFF calls); the rest are defensive for connector-served assets.
_REQUIRED_CONNECTOR_DIRECTIVES = ("script-src", "connect-src")
_OPTIONAL_CONNECTOR_DIRECTIVES = ("style-src", "img-src", "font-src")
_CONNECTOR_CSP_DIRECTIVES = (
    _REQUIRED_CONNECTOR_DIRECTIVES + _OPTIONAL_CONNECTOR_DIRECTIVES
)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _augment_csp_with_origins(
    policy: str, directives: tuple[str, ...], origins: list[str]
) -> tuple[str, list[str]]:
    """Append ``origins`` to each target directive present in ``policy``; return
    (new_policy, missing). Absent directives are left alone — injecting one would
    narrow whatever ``default-src`` already covers."""
    if not origins:
        return policy, []
    extra = " ".join(origins)
    targets = set(directives)
    seen: set[str] = set()
    out: list[str] = []
    for chunk in policy.split(";"):
        token = chunk.strip()
        if not token:
            continue
        name = token.split()[0].lower()
        if name in targets:
            seen.add(name)
            token = f"{token} {extra}"
        out.append(token)
    missing = [d for d in directives if d not in seen]
    return "; ".join(out), missing


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        hsts_enabled: Optional[bool] = None,
        frame_options_enabled: Optional[bool] = None,
        content_type_options_enabled: Optional[bool] = None,
        referrer_policy_enabled: Optional[bool] = None,
        csp_enabled: Optional[bool] = None,
        csp_policy: Optional[str] = None,
        hsts_max_age: Optional[int] = None,
    ):
        super().__init__(app)
        self.hsts_enabled = (
            _env_bool("VIGIL_HSTS_ENABLED", True)
            if hsts_enabled is None
            else hsts_enabled
        )
        self.frame_options_enabled = (
            _env_bool("VIGIL_FRAME_OPTIONS_ENABLED", True)
            if frame_options_enabled is None
            else frame_options_enabled
        )
        self.content_type_options_enabled = (
            _env_bool("VIGIL_CONTENT_TYPE_OPTIONS_ENABLED", True)
            if content_type_options_enabled is None
            else content_type_options_enabled
        )
        self.referrer_policy_enabled = (
            _env_bool("VIGIL_REFERRER_POLICY_ENABLED", True)
            if referrer_policy_enabled is None
            else referrer_policy_enabled
        )
        self.csp_enabled = (
            _env_bool("VIGIL_CSP_ENABLED", True)
            if csp_enabled is None
            else csp_enabled
        )
        self.csp_policy = csp_policy or os.getenv("VIGIL_CSP_POLICY") or DEFAULT_CSP
        # Admit allowlisted connector origins so the browser may import their
        # bundle + call their BFF. Read once at startup (restart to change).
        try:
            from services.extension_trust import connector_allowlist_origins

            connector_origins = connector_allowlist_origins()
        except Exception:  # pragma: no cover - defensive; never block startup
            connector_origins = []
        if connector_origins:
            self.csp_policy, missing = _augment_csp_with_origins(
                self.csp_policy, _CONNECTOR_CSP_DIRECTIVES, connector_origins
            )
            missing_required = [
                d for d in missing if d in _REQUIRED_CONNECTOR_DIRECTIVES
            ]
            if missing_required:
                logger.warning(
                    "CSP policy has no %s directive(s); page-extension connector "
                    "origins %s cannot be admitted and their bundle/BFF calls will "
                    "be blocked. Add these directives to VIGIL_CSP_POLICY.",
                    missing_required,
                    connector_origins,
                )
        self.hsts_max_age = (
            int(os.getenv("VIGIL_HSTS_MAX_AGE", "31536000"))
            if hsts_max_age is None
            else hsts_max_age
        )

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        if self.content_type_options_enabled:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")

        if self.frame_options_enabled:
            response.headers.setdefault("X-Frame-Options", "DENY")

        if self.referrer_policy_enabled:
            response.headers.setdefault(
                "Referrer-Policy", "strict-origin-when-cross-origin"
            )

        if self.csp_enabled:
            response.headers.setdefault("Content-Security-Policy", self.csp_policy)

        if self.hsts_enabled and self._is_https(request):
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={self.hsts_max_age}; includeSubDomains",
            )

        return response

    @staticmethod
    def _is_https(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        forwarded_proto = request.headers.get("x-forwarded-proto", "").lower()
        return forwarded_proto == "https"
