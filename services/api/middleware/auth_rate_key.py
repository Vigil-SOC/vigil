"""Rate-limit keys for auth routes.

A limit keyed only on the TCP peer becomes one bucket for every user when a
reverse proxy, ingress controller, or Docker's userland proxy is the peer.
These keys include the account (or the token's user) so one client cannot
spend another user's budget. The peer is still part of the anonymous key,
after proxy headers are applied, so missing-credential traffic stays bounded
per source once the proxy is trusted.
"""

import hashlib
import json
from typing import Optional

import jwt
from starlette.requests import Request

from core.auth.auth_cookies import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME
from core.auth.auth_service import JWT_ALGORITHM, JWT_SECRET_KEY
from services.api.middleware.client_address import client_host

_MAX_SUBJECT = 320


def _body(request: Request) -> dict:
    raw = getattr(request, "_body", None)
    if not raw:
        return {}
    if isinstance(raw, str):
        raw = raw.encode()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _subject(value: object) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value.strip().casefold() if ch.isprintable())
    return cleaned[:_MAX_SUBJECT]


def _token_user(token: str, expected_type: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("token_type") != expected_type:
        return None
    user_id = payload.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return None
    return user_id


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    parts = header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return ""


def login_rate_key(request: Request) -> str:
    subject = _subject(_body(request).get("username_or_email"))
    if subject:
        return f"login:{subject}"
    return f"login:missing:{client_host(request)}"


def refresh_rate_key(request: Request) -> str:
    token = request.cookies.get(REFRESH_COOKIE_NAME) or ""
    if not token:
        body_token = _body(request).get("refresh_token")
        token = body_token if isinstance(body_token, str) else ""
    user_id = _token_user(token, "refresh") if token else None
    if user_id:
        return f"refresh:{user_id}"
    return f"refresh:missing:{client_host(request)}"


def session_rate_key(request: Request) -> str:
    token = request.cookies.get(ACCESS_COOKIE_NAME) or _bearer(request)
    user_id = _token_user(token, "access") if token else None
    if user_id:
        return f"session:{user_id}"
    return f"session:missing:{client_host(request)}"


def reset_request_rate_key(request: Request) -> str:
    email = _subject(_body(request).get("email"))
    if email:
        return f"reset-request:{email}"
    return f"reset-request:missing:{client_host(request)}"


def reset_confirm_rate_key(request: Request) -> str:
    token = _body(request).get("token")
    if isinstance(token, str) and token.strip():
        digest = hashlib.sha256(token.strip().encode()).hexdigest()
        return f"reset-confirm:{digest}"
    return f"reset-confirm:missing:{client_host(request)}"


def bootstrap_rate_key(request: Request) -> str:
    username = _subject(_body(request).get("username"))
    if username:
        return f"bootstrap:{username}"
    return f"bootstrap:missing:{client_host(request)}"
