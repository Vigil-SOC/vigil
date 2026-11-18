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
import json
import logging
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

logger = logging.getLogger(__name__)

COOKIE = "vigil_sso"
LIFE = timedelta(minutes=5)
TIMEOUT = 5.0
# Clock skew allowed on exp and iat.
LEEWAY = 30
ASYMMETRIC = [
    "RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512", "EdDSA",
]  # fmt: skip

# The login page's messages (decision 7, plus a directory-sync refusal, #4423).
FAILED = "failed"
DEACTIVATED = "deactivated"
EMAIL_NOT_VERIFIED = "email_not_verified"
NOT_PROVISIONED = "not_provisioned"
# A break-glass account is never signed into through the provider (#4534).
BREAK_GLASS_EMAIL = "break_glass_email"
BREAK_GLASS_LINKED = "break_glass_linked"
CODES = (
    FAILED, DEACTIVATED, EMAIL_NOT_VERIFIED, NOT_PROVISIONED,
    BREAK_GLASS_EMAIL, BREAK_GLASS_LINKED,
)  # fmt: skip


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


_broken_logged = False


def required(current: Optional[sso_settings.SsoSettings] = None) -> bool:
    """Require single sign-on in force: the stored setting alone.

    It stays in force while SSO itself is broken (its secret unreadable), so a
    fault never lets passwords back in. Break-glass accounts and the host's
    ``python -m core.auth.require_sso_off`` are the way back (#4532, #4286).
    """
    global _broken_logged
    s = current or sso_settings.settings()
    if s.require_sso and not is_on(s):
        if not _broken_logged:
            logger.error(
                "Require single sign-on is on but single sign-on can't be used "
                "(client secret unreadable?): only break-glass accounts can sign "
                "in. Re-save the client secret, or run python -m core.auth.require_sso_off"
            )
        _broken_logged = True
    elif s.require_sso:
        _broken_logged = False
    return s.require_sso


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


# A voluntary request for what the judgement reads (epic #4751 decision 3): a
# provider never fails a sign-in over it. Never an essential acr, never acr_values.
CLAIMS = json.dumps(
    {"id_token": {"amr": None, "acr": None, "auth_time": None}}, separators=(",", ":")
)


def begin(
    p: Provider,
    confirm: Optional[dict] = None,
    max_age: Optional[int] = None,
    back: Optional[str] = None,
) -> tuple[str, str]:
    """The provider's authorization address and the signed cookie that binds it.

    ``confirm``: Confirm it's you for a sign-in already open (#4285). It rides in
    the cookie, and the provider is asked for a fresh sign-in. Every trip asks
    for amr, acr and auth_time; a sign-in also sends ``max_age`` (seconds), so
    the provider repeats one older than that. ``back``: the console path a
    sign-in returns to (#4789), already checked by the caller.
    """
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    held = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "exp": datetime.now(timezone.utc) + LIFE,
    }
    if confirm:
        held["confirm"] = confirm
    elif back:
        held["return"] = back
    cookie = key_ring.sign("sso", held)
    query = urlencode(
        {
            # Voluntary, so asked on every trip: a confirmation's token is judged too.
            "claims": CLAIMS,
            **(
                {"prompt": "login", "max_age": 0}
                if confirm
                else ({"max_age": max_age} if max_age is not None else {})
            ),
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
    if held.get(
        "typ"
    ):  # a method-change proof or a waiting sign-in is never a state cookie
        raise Refused("bad_state_cookie")
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
    # The signature now decides MFA as well as who: no RSA key under 2048 bits.
    if alg.startswith(("RS", "PS")) and getattr(key.key, "key_size", 0) < 2048:
        raise Refused("id_token_key_weak")
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


# Registered phishing-resistance values (OpenID Connect EAP ACR Values 1.0).
PHR = ("phr", "phrh")


def judged(claims: dict, absolute: timedelta) -> tuple[Optional[str], Optional[int]]:
    """How the verified ID token meets MFA (epic #4751 decisions 1 and 2): the
    ``sign_ins`` value (PROVIDER_PHR, PROVIDER_MFA) or None, and its auth_time.

    Only registered values count: ``acr`` phr/phrh, or ``mfa`` in ``amr``, with
    an auth_time inside ``absolute``. A provider's own labels, a missing claim
    and a missing or old auth_time count for nothing."""
    from core.auth import sign_ins

    at = claims.get("auth_time")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return None, None
    now = datetime.now(timezone.utc).timestamp()
    at = int(at)
    if at > now + LEEWAY or at < now - absolute.total_seconds():
        return None, at
    acr, amr = claims.get("acr"), claims.get("amr")
    if isinstance(acr, str) and acr in PHR:
        return sign_ins.PROVIDER_PHR, at
    if isinstance(amr, list) and "mfa" in amr:
        return sign_ins.PROVIDER_MFA, at
    return None, at


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


def sso_only(
    session: Session, user, current: Optional[sso_settings.SsoSettings] = None
) -> bool:
    """Signs in only through the provider (epic #4751 decision 8): Require single
    sign-on in force, or no local password and linked to a provider. Never a
    break-glass account, whose password is the way in when SSO is not.
    ``current``: the settings, read already on ``session`` (an upgrade revision)."""
    if getattr(user, "break_glass_at", None) is not None:
        return False
    if current.require_sso if current is not None else required():
        return True
    if user.password_hash:
        return False
    q = session.query(SsoLink.principal_id).filter(
        SsoLink.principal_id == user.principal_id
    )
    return q.first() is not None


# A provider confirmation for a method change (#4756): stands in for the password.
PROOF = "sso_method_proof"


def issue_proof(sign_in_id: str, principal_id: str) -> str:
    """Signed under ``sso``, for this sign-in and person, good for ``LIFE``."""
    return key_ring.sign(
        "sso",
        {
            "typ": PROOF,
            "sid": sign_in_id,
            "principal": principal_id,
            "exp": datetime.now(timezone.utc) + LIFE,
        },
    )


def proof_holds(
    raw: Optional[str], sign_in_id: Optional[str], principal_id: str
) -> bool:
    """Whether ``raw`` is a live proof for exactly this sign-in and person. Its type
    keeps it apart from the state cookie, which is signed under the same purpose."""
    if not raw or not sign_in_id:
        return False
    try:
        held = jwt.decode(
            raw,
            key_ring.token_key("sso", raw),
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except (jwt.PyJWTError, key_ring.KeyRingError):
        return False
    return (
        held.get("typ") == PROOF
        and held.get("sid") == sign_in_id
        and held.get("principal") == principal_id
    )


# A sign-in waiting on the person's Vigil factor after the provider (#4757).
PENDING = "sso_pending"


def hold(pending: dict) -> str:
    """The waiting sign-in, signed under ``sso`` for ``LIFE``: what finishing it
    needs and nothing more (never the groups claim)."""
    return key_ring.sign(
        "sso", {**pending, "typ": PENDING, "exp": datetime.now(timezone.utc) + LIFE}
    )


def waiting(raw: Optional[str]) -> Optional[dict]:
    """The waiting sign-in ``raw`` holds, while it lives; never a state cookie or a proof."""
    if not raw:
        return None
    try:
        held = jwt.decode(
            raw,
            key_ring.token_key("sso", raw),
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except (jwt.PyJWTError, key_ring.KeyRingError):
        return None
    return held if held.get("typ") == PENDING else None


def linked(session: Session, issuer: str, subject: str) -> Optional[SsoLink]:
    return (
        session.query(SsoLink)
        .filter(SsoLink.issuer == issuer, SsoLink.subject == subject)
        .first()
    )


def linked_by_directory(
    session: Session, issuer: str, directory_id: str
) -> Optional[SsoLink]:
    """The link whose externalId is ``directory_id`` at this issuer."""
    if not directory_id:
        return None
    return (
        session.query(SsoLink)
        .filter(SsoLink.issuer == issuer, SsoLink.directory_id == directory_id)
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
