"""Signing in through the single sign-on provider: OpenID Connect code flow (#4284).

Settings come only from ``sso_settings`` (#4283). The provider's endpoints are
read from its discovery document at each sign-in, never stored. The browser is
bound to its attempt by a short cookie signed under the key ring's ``sso``
purpose, holding the state, nonce and PKCE verifier.

Every refusal is a ``Refused`` with a fixed reason for the trail and one of three
codes for the login page. A library's own message, the code, a token or the
secret never reaches either.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional
from urllib.parse import quote, urlencode

import httpx
import jwt
from sqlalchemy.orm import Session

from core.auth import key_ring, sso_settings
from core.auth.password_reset import default_base_url
from core.config import get_settings
from core.secrets import get_secret
from core.storage.models import SsoLink

COOKIE = "vigil_sso"
LIFE = timedelta(minutes=5)
TIMEOUT = 5.0
# Clock skew allowed on exp and iat.
LEEWAY = 30
ASYMMETRIC = [
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512", "EdDSA",
]  # fmt: skip

# The login page's three messages (decision 7).
FAILED = "failed"
DEACTIVATED = "deactivated"
EMAIL_NOT_VERIFIED = "email_not_verified"
CODES = (FAILED, DEACTIVATED, EMAIL_NOT_VERIFIED)


class Refused(Exception):
    """A sign-in through the provider that does not go ahead."""

    def __init__(
        self,
        reason: str,
        code: str = FAILED,
        principal: Optional[str] = None,
        attempted: str = "",
    ):
        super().__init__(reason)
        self.reason, self.code, self.principal = reason, code, principal
        # The email the token named, when it got that far: the trail's attempted name.
        self.attempted = attempted


class Provider(NamedTuple):
    settings: sso_settings.SsoSettings
    secret: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    auth_methods: list


def is_on(current: Optional[sso_settings.SsoSettings] = None) -> bool:
    """Enabled, reached once (an issuer), with a client ID and a stored secret."""
    s = current or sso_settings.settings()
    return bool(
        s.enabled and s.issuer and s.client_id and get_secret(sso_settings.SECRET_KEY)
    )


def required(current: Optional[sso_settings.SsoSettings] = None) -> bool:
    """Require single sign-on in force: on, and SSO itself working, so a broken row locks nobody out."""
    s = current or sso_settings.settings()
    return bool(s.require_sso and is_on(s))


# The one detail a refused password sign-in gets while it is required.
REQUIRED_DETAIL = "Your organization signs in with SSO"


def redirect_uri() -> str:
    """The address the provider sends the browser back to: the one the card shows."""
    return default_base_url(get_settings()).rstrip("/") + sso_settings.CALLBACK_PATH


def provider() -> Provider:
    """The provider in force, its endpoints fetched now from discovery."""
    s = sso_settings.settings()
    secret = get_secret(sso_settings.SECRET_KEY) or ""
    if not is_on(s):
        raise Refused("sso_off")
    try:
        res = httpx.get(s.discovery_url, timeout=TIMEOUT, follow_redirects=False)
        doc = res.json() if res.is_success else None
    except Exception:  # noqa: BLE001 - any failure is the one reason
        doc = None
    if not isinstance(doc, dict):
        raise Refused("discovery_unreachable")
    if doc.get("issuer") != s.issuer:
        raise Refused("issuer_changed")
    endpoints = [doc.get(k) for k in sso_settings.REQUIRED_ENDPOINTS]
    if not all(isinstance(e, str) and e for e in endpoints):
        raise Refused("discovery_incomplete")
    methods = doc.get("token_endpoint_auth_methods_supported")
    return Provider(s, secret, *endpoints, methods if isinstance(methods, list) else [])


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def begin(p: Provider) -> tuple[str, str]:
    """The provider's authorization address and the signed cookie that binds it."""
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    cookie = key_ring.sign(
        "sso",
        {
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "exp": datetime.now(timezone.utc) + LIFE,
        },
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": p.settings.client_id,
            "redirect_uri": redirect_uri(),
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
    )
    sep = "&" if "?" in p.authorization_endpoint else "?"
    return f"{p.authorization_endpoint}{sep}{query}", cookie


def binding(raw: Optional[str], state: Optional[str]) -> dict:
    """The cookie's state, nonce and verifier, once its state matches the reply's."""
    if not raw:
        raise Refused("no_state_cookie")
    try:
        held = jwt.decode(
            raw,
            key_ring.token_key("sso", raw),
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except (jwt.PyJWTError, key_ring.KeyRingError):
        raise Refused("bad_state_cookie") from None
    if not state or not hmac.compare_digest(str(held.get("state", "")), state):
        raise Refused("state_mismatch")
    return held


def exchange(p: Provider, code: str, verifier: str) -> str:
    """The ID token the provider gives for ``code``."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "code_verifier": verifier,
    }
    headers = {"Accept": "application/json"}
    if p.auth_methods and "client_secret_basic" not in p.auth_methods:
        data.update(client_id=p.settings.client_id, client_secret=p.secret)
    else:
        # RFC 6749 2.3.1: each part form-encoded before Basic.
        pair = f"{quote(p.settings.client_id, safe='')}:{quote(p.secret, safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    try:
        res = httpx.post(
            p.token_endpoint,
            data=data,
            headers=headers,
            timeout=TIMEOUT,
            follow_redirects=False,
        )
        body = res.json() if res.is_success else None
    except Exception:  # noqa: BLE001 - the reason is fixed
        body = None
    token = body.get("id_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise Refused("code_exchange_failed")
    return token


# One key-set client per JWKS address, a few at most: each caches its keys and
# refetches on an unknown kid.
_JWKS: "OrderedDict[str, jwt.PyJWKClient]" = OrderedDict()
_JWKS_MAX = 4
_jwks_lock = threading.Lock()


def _jwks(uri: str) -> jwt.PyJWKClient:
    with _jwks_lock:
        client = _JWKS.pop(uri, None) or jwt.PyJWKClient(
            uri, cache_keys=True, max_cached_keys=16, lifespan=300, timeout=TIMEOUT
        )
        _JWKS[uri] = client
        while len(_JWKS) > _JWKS_MAX:
            _JWKS.popitem(last=False)
        return client


def verify(p: Provider, id_token: str, nonce: str) -> dict[str, Any]:
    """The ID token's claims, once signature, issuer, audience, expiry and nonce hold."""
    try:
        alg = jwt.get_unverified_header(id_token).get("alg")
    except jwt.PyJWTError:
        raise Refused("id_token_malformed") from None
    if alg not in ASYMMETRIC:
        raise Refused("id_token_algorithm")
    try:
        key = _jwks(p.jwks_uri).get_signing_key_from_jwt(id_token)
    except jwt.PyJWTError:
        raise Refused("id_token_key_unknown") from None
    try:
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=[alg],
            audience=p.settings.client_id,
            issuer=p.settings.issuer,
            leeway=LEEWAY,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise Refused("id_token_expired") from None
    except jwt.InvalidAudienceError:
        raise Refused("id_token_audience") from None
    except jwt.InvalidIssuerError:
        raise Refused("id_token_issuer") from None
    except jwt.PyJWTError:
        raise Refused("id_token_invalid") from None
    aud = claims.get("aud")
    if (
        isinstance(aud, list)
        and len(aud) > 1
        and claims.get("azp") != p.settings.client_id
    ):
        raise Refused("id_token_audience")
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise Refused("id_token_nonce")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise Refused("no_subject")
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email:
        raise Refused("no_email")
    if len(email.strip()) > 200:  # users.email
        raise Refused("email_too_long")
    if claims.get("email_verified") is False:
        raise Refused("email_not_verified", EMAIL_NOT_VERIFIED, attempted=email)
    return claims


def groups_of(claims: dict, claim: str) -> Optional[list[str]]:
    """The groups claim as strings, or None when it is missing or unusable."""
    value = claims.get(claim)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(g, str) for g in value):
        return value
    return None


def linked_principals(session: Session) -> set[str]:
    return {pid for (pid,) in session.query(SsoLink.principal_id).all()}


def linked(session: Session, issuer: str, subject: str) -> Optional[SsoLink]:
    return (
        session.query(SsoLink)
        .filter(SsoLink.issuer == issuer, SsoLink.subject == subject)
        .first()
    )


def link(session: Session, principal_id: str, issuer: str, subject: str) -> SsoLink:
    row = SsoLink(
        link_id=f"sso-{uuid.uuid4().hex}",
        principal_id=principal_id,
        issuer=issuer,
        subject=subject,
    )
    session.add(row)
    return row
