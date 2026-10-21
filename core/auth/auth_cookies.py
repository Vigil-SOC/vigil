"""
Auth cookie helpers.

Centralizes the HttpOnly / Secure / SameSite flags so every endpoint that
sets or clears auth cookies agrees on the attributes. Reading attributes
from env at call time means flipping `VIGIL_COOKIE_SECURE=false` in local
dev doesn't require a restart cycle through the router.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Response

from core.config import get_settings

logger = logging.getLogger(__name__)


ACCESS_COOKIE_NAME = "access_token"
REFRESH_COOKIE_NAME = "refresh_token"

# Unprefixed Path values. Prefixed at set/clear time from vigil_context_path
# so a subdirectory deploy (example.com/vigil) still sends cookies.
_ACCESS_COOKIE_PATH = "/"
# Covers every auth route, including logout, and nothing under the rest of
# /api. The previous path was /api/auth/refresh, so a browser never attached
# the cookie to logout and the refresh token stayed valid after sign-out.
_REFRESH_COOKIE_PATH = "/api/auth/"
_LEGACY_REFRESH_COOKIE_PATH = "/api/auth/refresh"


def context_path_prefix() -> str:
    """Normalized VIGIL_CONTEXT_PATH (no trailing slash). Empty at root."""
    return get_settings().vigil_context_path.rstrip("/")


def cookie_root_path() -> str:
    """Path for access + CSRF cookies: `{prefix}/` or `/`."""
    prefix = context_path_prefix()
    return f"{prefix}/" if prefix else _ACCESS_COOKIE_PATH


def refresh_cookie_path() -> str:
    """Path for the refresh cookie: `{prefix}/api/auth/`."""
    return f"{context_path_prefix()}{_REFRESH_COOKIE_PATH}"


def legacy_refresh_cookie_path() -> str:
    """Path of refresh cookies issued before logout was inside the path."""
    return f"{context_path_prefix()}{_LEGACY_REFRESH_COOKIE_PATH}"


def _cookie_secure() -> bool:
    # Default true so a misconfiguration in prod fails safe. Local HTTP dev
    # must explicitly set VIGIL_COOKIE_SECURE=false.
    return get_settings().vigil_cookie_secure


def _cookie_samesite() -> str:
    raw = get_settings().vigil_cookie_samesite.strip().lower()
    if raw not in ("strict", "lax", "none"):
        logger.warning(
            "Invalid VIGIL_COOKIE_SAMESITE=%r, falling back to 'strict'", raw
        )
        return "strict"
    return raw


def _ttl_seconds(exp_ts: Optional[int]) -> Optional[int]:
    if exp_ts is None:
        return None
    ttl = exp_ts - int(datetime.now(tz=timezone.utc).timestamp())
    return ttl if ttl > 0 else None


def set_auth_cookies(
    response: Response,
    access_token: str,
    refresh_token: str,
    *,
    access_exp: Optional[int] = None,
    refresh_exp: Optional[int] = None,
) -> None:
    """Set both auth cookies with matching attributes."""
    secure = _cookie_secure()
    samesite = _cookie_samesite()

    response.set_cookie(
        ACCESS_COOKIE_NAME,
        access_token,
        max_age=_ttl_seconds(access_exp),
        httponly=True,
        secure=secure,
        samesite=samesite,
        path=cookie_root_path(),
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=_ttl_seconds(refresh_exp),
        httponly=True,
        secure=secure,
        samesite=samesite,
        path=refresh_cookie_path(),
    )
    # A different path is a different cookie. Setting the new one leaves a
    # pre-upgrade cookie in the browser; drop that path too.
    _delete_refresh_cookie(response, legacy_refresh_cookie_path(), secure, samesite)


def _delete_refresh_cookie(
    response: Response, path: str, secure: bool, samesite: str
) -> None:
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=path,
        secure=secure,
        samesite=samesite,
    )


def clear_auth_cookies(response: Response) -> None:
    """Clear both auth cookies. Matching attributes required or browsers
    won't recognize the clear. The legacy refresh path is cleared as well
    so a browser that signed in before the path widened drops that cookie."""
    secure = _cookie_secure()
    samesite = _cookie_samesite()
    response.delete_cookie(
        ACCESS_COOKIE_NAME,
        path=cookie_root_path(),
        secure=secure,
        samesite=samesite,
    )
    for path in (refresh_cookie_path(), legacy_refresh_cookie_path()):
        _delete_refresh_cookie(response, path, secure, samesite)
