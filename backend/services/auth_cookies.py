"""
Auth cookie helpers.

Centralizes the HttpOnly / Secure / SameSite flags so every endpoint that
sets or clears auth cookies agrees on the attributes. Reading attributes
from env at call time means flipping `VIGIL_COOKIE_SECURE=false` in local
dev doesn't require a restart cycle through the router.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import Response

logger = logging.getLogger(__name__)


ACCESS_COOKIE_NAME = "access_token"
REFRESH_COOKIE_NAME = "refresh_token"

# Path scoping: refresh cookie is only sent to the refresh endpoint so it
# isn't exposed to every API call the browser makes.
ACCESS_COOKIE_PATH = "/"
REFRESH_COOKIE_PATH = "/api/auth/refresh"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _cookie_secure() -> bool:
    # Default true so a misconfiguration in prod fails safe. Local HTTP dev
    # must explicitly set VIGIL_COOKIE_SECURE=false.
    return _env_bool("VIGIL_COOKIE_SECURE", True)


def _cookie_samesite() -> str:
    raw = (os.getenv("VIGIL_COOKIE_SAMESITE") or "strict").strip().lower()
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
        path=ACCESS_COOKIE_PATH,
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=_ttl_seconds(refresh_exp),
        httponly=True,
        secure=secure,
        samesite=samesite,
        path=REFRESH_COOKIE_PATH,
    )


def clear_auth_cookies(response: Response) -> None:
    """Clear both auth cookies. Matching attributes required or browsers
    won't recognize the clear."""
    secure = _cookie_secure()
    samesite = _cookie_samesite()
    response.delete_cookie(
        ACCESS_COOKIE_NAME,
        path=ACCESS_COOKIE_PATH,
        secure=secure,
        samesite=samesite,
    )
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        secure=secure,
        samesite=samesite,
    )
