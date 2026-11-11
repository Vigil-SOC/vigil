"""Single sign-on settings: which OpenID Connect provider people sign in through (#4283).

One ``system_config`` row holds the document, the fields of ``SsoSettings``
plus its ``version``, read and written the way ``session_policy`` is. The
client secret is never in the row: it lives under ``SECRET_KEY`` in the secrets
store, and the read model says only whether one is set.
"""

import json
from datetime import datetime
from typing import NamedTuple, Optional

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from core.storage.models import SystemConfig
from core.storage.unit_of_work import unit_of_work

KEY = "auth.sso"
SECRET_KEY = "VIGIL_SSO_CLIENT_SECRET"
WELL_KNOWN = "/.well-known/openid-configuration"
CALLBACK_PATH = "/api/auth/sso/callback"
DEFAULT_ROLE = "role-viewer"
DISCOVERY_TIMEOUT = 5.0

# Fixed refusal reasons: a library's own message never reaches a response or the trail.
UNREACHABLE = "The discovery URL could not be reached"
NOT_JSON = "The discovery URL did not return a JSON document"
ISSUER_MISMATCH = "The provider's issuer does not match the discovery URL"
NOT_DISCOVERY_URL = f"The discovery URL must end in {WELL_KNOWN}"
REQUIRED_ENDPOINTS = ("authorization_endpoint", "token_endpoint", "jwks_uri")


def missing(endpoint: str) -> str:
    return f"The discovery document has no {endpoint}"


class GroupRole(BaseModel):
    group: str = Field(min_length=1, max_length=256)
    role_id: str = Field(min_length=1)


class SsoSettings(BaseModel):
    enabled: bool = False
    discovery_url: str = Field("", max_length=2048)
    client_id: str = Field("", max_length=512)
    # The provider's issuer from the last successful discovery check; None: never reached.
    issuer: Optional[str] = None
    # Checked top to bottom at sign-in; the first match wins.
    group_roles: list[GroupRole] = Field(default_factory=list, max_length=200)
    default_role_id: str = DEFAULT_ROLE
    groups_claim: str = Field("groups", min_length=1, max_length=128)
    # Password sign-in and reset links are refused for people while on (#4286).
    require_sso: bool = False
    # When the discovery URL or client ID last changed; None: before this was kept,
    # which no sign-in qualifies against until the next such save.
    provider_changed_at: Optional[datetime] = None


class Stored(NamedTuple):
    settings: SsoSettings
    version: int
    updated_by: Optional[str]
    updated_at: object


def read_row(session: Session, lock: bool = False) -> tuple[Stored, bool]:
    """The stored document (defaults when no row) and whether a row exists."""
    query = (
        session.query(SystemConfig).filter(SystemConfig.key == KEY).populate_existing()
    )
    row = (query.with_for_update() if lock else query).first()
    if row is None:
        return Stored(SsoSettings(), 0, None, None), False
    value = dict(row.value or {})
    version = int(value.pop("version", 0))
    return Stored(SsoSettings(**value), version, row.updated_by, row.updated_at), True


def settings() -> SsoSettings:
    """The settings in force now, read every call."""
    with unit_of_work() as session:
        try:
            return read_row(session)[0].settings
        except (ValidationError, TypeError, ValueError):
            # A row edited by hand into something invalid: the defaults hold.
            return SsoSettings()


class DiscoveryRefused(Exception):
    """A discovery check that failed; ``reason`` is one of the fixed messages above."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def expected_issuer(discovery_url: str) -> str:
    """The issuer OpenID Connect discovery implies: the URL without the well-known suffix."""
    url = discovery_url.strip().rstrip("/")
    if not url.endswith(WELL_KNOWN):
        raise DiscoveryRefused(NOT_DISCOVERY_URL)
    return url[: -len(WELL_KNOWN)]


def discover(discovery_url: str) -> str:
    """Fetch the discovery document and return its issuer, or raise DiscoveryRefused."""
    expected = expected_issuer(discovery_url)
    try:
        res = httpx.get(
            discovery_url.strip(), timeout=DISCOVERY_TIMEOUT, follow_redirects=False
        )
    except Exception:  # noqa: BLE001 - any failure to reach it is the one reason
        raise DiscoveryRefused(UNREACHABLE) from None
    if not res.is_success:
        raise DiscoveryRefused(UNREACHABLE)
    try:
        doc = res.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise DiscoveryRefused(NOT_JSON) from None
    if not isinstance(doc, dict):
        raise DiscoveryRefused(NOT_JSON)
    for endpoint in REQUIRED_ENDPOINTS:
        if not isinstance(doc.get(endpoint), str) or not doc[endpoint]:
            raise DiscoveryRefused(missing(endpoint))
    issuer = doc.get("issuer")
    if not isinstance(issuer, str) or issuer.rstrip("/") != expected:
        raise DiscoveryRefused(ISSUER_MISMATCH)
    return issuer
