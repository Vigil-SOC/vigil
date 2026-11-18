"""Vigil's OAuth authorization server: clients, registration and authorize (#4787).

Implements the MCP SDK's ``OAuthAuthorizationServerProvider``; the SDK's route
builders do the protocol and ``services/api/oauth_surface.py`` mounts them at
the host root. Vigil supplies storage and policy:

* **Registration** accepts loopback redirect URIs (any port) and the operator's
  allowlist (``mcp.oauth_redirect_allowlist``, empty by default). Anything else
  is refused and the refusal is on the trail. Every client is public: Vigil
  issues no secret and relies on PKCE (S256), so nothing secret is stored.
* **Authorize** records a pending request and sends the browser to the console's
  consent page, which the sibling (#4770) serves. Codes and tokens are #4769;
  until then those provider methods raise ``NotImplementedError``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
)
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from sqlalchemy import exists, func

from core.auth import trail_events
from core.integrations.mcp.surface import (
    REDIRECT_ALLOWLIST_SETTING,
    oauth_redirect_allowlist,
    oauth_urls,
)
from core.storage.models import McpCredential, OAuthClient, OAuthPending
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

SCOPE = "mcp"
PENDING_TTL = timedelta(minutes=10)
CLIENT_UNUSED = timedelta(days=30)
# Registration is unauthenticated: a cap keeps a flood from filling the table.
MAX_CLIENTS = 1000
MAX_REDIRECT_URIS = 10
# Registration is anonymous and lands on the trail: lengths are capped first.
MAX_URI = 2000
MAX_NAME = 200

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}

# The caller's address, bound by the gate for the trail; the provider sees no request.
request_address: ContextVar[Optional[str]] = ContextVar(
    "vigil_oauth_address", default=None
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _loopback_key(uri: str) -> Optional[tuple]:
    """(scheme, host, path, query) of a loopback URI, port left out; None otherwise."""
    parts = urlsplit(uri)
    if parts.scheme not in ("http", "https") or parts.hostname not in _LOOPBACK:
        return None
    return (parts.scheme, parts.hostname, parts.path or "/", parts.query)


def redirect_allowed(uri: str, allowlist: Optional[list[str]] = None) -> bool:
    """Loopback (RFC 8252, any port) or named exactly on the operator's list."""
    if urlsplit(uri).fragment:
        return False
    if _loopback_key(uri) is not None:
        return True
    return uri in (oauth_redirect_allowlist() if allowlist is None else allowlist)


class VigilClient(OAuthClientInformationFull):
    """A stored client. The SDK matches redirect URIs exactly, which breaks a
    native client that picks a loopback port per run; this matches loopback on
    host and path, any port (RFC 8252 §7.3)."""

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is not None:
            key = _loopback_key(str(redirect_uri))
            if key is not None and any(
                _loopback_key(str(r)) == key for r in self.redirect_uris or []
            ):
                return redirect_uri
        return super().validate_redirect_uri(redirect_uri)


def _client_from(row: OAuthClient) -> VigilClient:
    # A URI the operator has since taken off the list no longer counts.
    allowlist = oauth_redirect_allowlist()
    uris = [u for u in row.redirect_uris if redirect_allowed(u, allowlist)]
    return VigilClient(
        client_id=row.client_id,
        client_name=row.name,
        redirect_uris=uris or None,
        token_endpoint_auth_method=row.auth_method,
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=SCOPE,
        client_id_issued_at=int(row.created_at.timestamp()) if row.created_at else None,
    )


def _refuse_registration(description: str, **fields) -> None:
    trail_events.refused(
        "oauth.client_registration_refused",
        beside={"source_ip": request_address.get()},
        **fields,
    )
    raise RegistrationError("invalid_client_metadata", description)


def _register(client: OAuthClientInformationFull) -> None:
    uris = [str(u) for u in client.redirect_uris or []]
    if len(uris) > MAX_REDIRECT_URIS or any(len(u) > MAX_URI for u in uris):
        raise RegistrationError(
            "invalid_client_metadata",
            f"At most {MAX_REDIRECT_URIS} redirect URIs of up to {MAX_URI} "
            "characters may be registered.",
        )
    name = client.client_name = (client.client_name or "")[:MAX_NAME] or None
    allowlist = oauth_redirect_allowlist()
    for uri in uris:
        if not redirect_allowed(uri, allowlist):
            _refuse_registration(
                f"Redirect URI {uri} is not allowed. Vigil accepts loopback "
                "addresses and the URIs an operator adds to "
                f"{REDIRECT_ALLOWLIST_SETTING}.",
                redirect_uri=uri,
                allow_in=REDIRECT_ALLOWLIST_SETTING,
            )
    # Public client, whatever was asked for (RFC 7591 §3.2.1 lets the server
    # substitute): PKCE protects the code and there is no secret to keep.
    client.token_endpoint_auth_method = "none"
    client.client_secret = None
    client.client_secret_expires_at = None
    client.scope = SCOPE
    with unit_of_work() as session:
        if session.query(func.count(OAuthClient.client_id)).scalar() >= MAX_CLIENTS:
            raise RegistrationError(
                "invalid_client_metadata",
                "This Vigil has as many registered clients as it allows.",
            )
        session.add(
            OAuthClient(
                client_id=client.client_id,
                name=name,
                redirect_uris=uris,
                auth_method=client.token_endpoint_auth_method,
                source="dynamic",
            )
        )
        trail_events.record(
            session,
            "oauth.client_registered",
            {"source_ip": request_address.get()},
            client_id=client.client_id,
            client_name=name,
            redirect_uris=uris,
        )


def _authorize(client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
    urls = oauth_urls()
    resource = params.resource or urls["resource"]
    if resource.rstrip("/") != urls["resource"]:
        raise AuthorizeError(
            "invalid_target", f"This server issues tokens only for {urls['resource']}."
        )
    # RFC 7636 §4.2: an S256 challenge is 43 characters; the column holds 128.
    if not 43 <= len(params.code_challenge) <= 128:
        raise AuthorizeError(
            "invalid_request", "code_challenge must be 43-128 characters."
        )
    request_id = secrets.token_urlsafe(32)
    now = utcnow()
    with unit_of_work() as session:
        session.add(
            OAuthPending(
                request_hash=_hash(request_id),
                client_id=client.client_id,
                redirect_uri=str(params.redirect_uri),
                redirect_uri_explicit=params.redirect_uri_provided_explicitly,
                state=params.state,
                code_challenge=params.code_challenge,
                scopes=" ".join(params.scopes or [SCOPE]),
                resource=urls["resource"],
                created_at=now,
                expires_at=now + PENDING_TTL,
            )
        )
        session.query(OAuthClient).filter(
            OAuthClient.client_id == client.client_id
        ).update({OAuthClient.last_used_at: now})
    return f"{urls['base']}/oauth/authorize?request={request_id}"


def _get_client(client_id: str) -> Optional[VigilClient]:
    with unit_of_work() as session:
        row = session.get(OAuthClient, client_id)
        return _client_from(row) if row else None


class VigilOAuthProvider:
    """``OAuthAuthorizationServerProvider`` over Vigil's tables. Each method runs
    its database work off the event loop."""

    async def get_client(self, client_id: str) -> Optional[VigilClient]:
        try:
            uuid.UUID(client_id)
        except (TypeError, ValueError):
            return None  # not one of ours; no query for it
        return await asyncio.to_thread(_get_client, client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await asyncio.to_thread(_register, client_info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        return await asyncio.to_thread(_authorize, client, params)

    # Codes and tokens are #4769. The gate answers these 501.
    async def load_authorization_code(self, client, authorization_code):
        raise NotImplementedError

    async def exchange_authorization_code(self, client, authorization_code):
        raise NotImplementedError

    async def load_refresh_token(self, client, refresh_token):
        raise NotImplementedError

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        raise NotImplementedError

    async def load_access_token(self, token):
        raise NotImplementedError

    async def revoke_token(self, token):
        raise NotImplementedError


def purge(now: Optional[datetime] = None) -> tuple[int, int]:
    """Delete expired pending requests, and clients unused for 30 days that hold
    no grant. Returns (pending, clients) deleted. Run by the token sweep."""
    now = now or utcnow()
    with unit_of_work() as session:
        pending = (
            session.query(OAuthPending)
            .filter(OAuthPending.expires_at <= now)
            .delete(synchronize_session=False)
        )
        clients = (
            session.query(OAuthClient)
            .filter(
                func.coalesce(OAuthClient.last_used_at, OAuthClient.created_at)
                <= now - CLIENT_UNUSED,
                ~exists().where(McpCredential.oauth_client_id == OAuthClient.client_id),
                # Expired requests are gone by now; a live one keeps its client.
                ~exists().where(OAuthPending.client_id == OAuthClient.client_id),
            )
            .delete(synchronize_session=False)
        )
    if pending or clients:
        logger.info(
            "OAuth purge: %d expired request(s), %d unused client(s)", pending, clients
        )
    return pending, clients
