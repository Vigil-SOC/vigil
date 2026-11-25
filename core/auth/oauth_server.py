"""Vigil's OAuth authorization server: clients, consent, codes and tokens (#4787, #4788).

Implements the MCP SDK's ``OAuthAuthorizationServerProvider``; the SDK's route
builders do the protocol and ``services/api/oauth_surface.py`` mounts them at
the host root. Vigil supplies storage and policy:

* **Registration** accepts loopback redirect URIs (any port) and the operator's
  allowlist (``mcp.oauth_redirect_allowlist``, empty by default). Anything else
  is refused and the refusal is on the trail. Every client is public: Vigil
  issues no secret and relies on PKCE (S256), so nothing secret is stored.
* **Authorize** records a pending request and sends the browser to the console's
  consent page (#4770), which calls :func:`decide` through
  ``/api/oauth/authorizations/{id}``. Approval puts the person and a 60-second
  code on the pending row; denial deletes it.
* **Tokens.** Exchanging the code deletes the pending row (so a code works once)
  and creates the grant: an ``McpCredential`` row (epic #4763 D4) whose
  ``token_hash`` is the hour-long ``vgl_oat_`` access token. Each refresh rotates
  both tokens; presenting the previous ``vgl_ort_`` again ends the grant. Only
  hashes are stored.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from sqlalchemy import exists, func

from core.audit import trail
from core.auth import mcp_credential_service as tokens
from core.auth import token_notices, token_policy, trail_events
from core.integrations.mcp.surface import (
    REDIRECT_ALLOWLIST_SETTING,
    oauth_redirect_allowlist,
    oauth_urls,
)
from core.storage.models import (
    McpCredential,
    OAuthClient,
    OAuthPending,
    SignIn,
    User,
)
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

SCOPE = "mcp"
PENDING_TTL = timedelta(minutes=10)
CLIENT_UNUSED = timedelta(days=30)
CODE_TTL = timedelta(seconds=60)
ACCESS_TTL = timedelta(hours=1)
# A grant lasts the holder's token lifetime, never more than this.
GRANT_MAX = timedelta(days=tokens.PERSON_DEFAULT_DAYS)
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

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> Optional[VigilCode]:
        return await asyncio.to_thread(_load_code, client.client_id, authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: VigilCode
    ) -> OAuthToken:
        try:
            return await asyncio.to_thread(_exchange_code, authorization_code)
        except _Refused as exc:
            raise TokenError("invalid_grant", str(exc)) from None

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> Optional[VigilRefreshToken]:
        return await asyncio.to_thread(_load_refresh, client.client_id, refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: VigilRefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        try:
            return await asyncio.to_thread(_exchange_refresh, refresh_token)
        except _Refused as exc:
            raise TokenError("invalid_grant", str(exc)) from None

    async def load_access_token(self, token: str) -> Optional[VigilAccessToken]:
        return await asyncio.to_thread(_load_access, token)

    async def revoke_token(self, token: VigilAccessToken | VigilRefreshToken) -> None:
        await asyncio.to_thread(_revoke, token.credential_id)

    async def exchange_identity_assertion(self, client, params) -> OAuthToken:
        raise TokenError(
            "unsupported_grant_type", "The JWT bearer grant is not supported"
        )


def _epoch(value: datetime) -> int:
    return int(value.replace(tzinfo=timezone.utc).timestamp())  # naive UTC


class PendingNotFound(Exception):
    """No undecided, unexpired request has this id."""


def _undecided(session, request_id: str, lock: bool = False) -> OAuthPending:
    query = session.query(OAuthPending).filter(
        OAuthPending.request_hash == _hash(request_id),
        OAuthPending.user_id.is_(None),
        OAuthPending.expires_at > utcnow(),
    )
    row = (query.with_for_update() if lock else query).first()
    if row is None:
        raise PendingNotFound
    return row


def pending_request(request_id: str) -> dict:
    """What the consent page shows: the client, where it returns to, and the
    resource. PendingNotFound once decided, expired or unknown."""
    with unit_of_work() as session:
        row = _undecided(session, request_id)
        client = session.get(OAuthClient, row.client_id)
        return {
            "client_id": row.client_id,
            "client_name": client.name if client else None,
            "redirect_host": urlsplit(row.redirect_uri).netloc,
            "resource": row.resource,
            "expires_at": row.expires_at.isoformat(),
        }


def decide(
    request_id: str, user: User, approve: bool, sign_in_id: Optional[str] = None
) -> str:
    """Approve or deny a pending request as ``user``; returns the client's redirect
    URL with ``code`` or ``error=access_denied``, ``state`` and ``iss``. The caller
    has applied the minting rules. An approval's code is good only while
    ``sign_in_id`` stays open. PendingNotFound once decided or expired."""
    issuer = oauth_urls()["issuer"]
    with unit_of_work() as session:
        row = _undecided(session, request_id, lock=True)
        fields = dict(
            actor=user.principal_id,
            principal=user.principal_id,
            client_id=row.client_id,
        )
        if not approve:
            redirect, state = row.redirect_uri, row.state
            session.delete(row)
            trail_events.record(session, "oauth.consent_denied", **fields)
            return construct_redirect_uri(
                redirect, error="access_denied", state=state, iss=issuer
            )
        code = secrets.token_urlsafe(32)
        row.user_id = user.user_id
        row.sign_in_id = sign_in_id
        row.code_hash = _hash(code)
        row.expires_at = utcnow() + CODE_TTL
        trail_events.record(
            session, "oauth.consent_granted", sign_in=sign_in_id, **fields
        )
        return construct_redirect_uri(
            row.redirect_uri, code=code, state=row.state, iss=issuer
        )


class _Refused(Exception):
    """An exchange refused as ``invalid_grant``. The SDK's TokenError is frozen, so
    it can't pass through a context manager; the provider converts this one."""


class VigilCode(AuthorizationCode):
    pass


class VigilRefreshToken(RefreshToken):
    credential_id: str


class VigilAccessToken(AccessToken):
    credential_id: str


def _issue(record: McpCredential, now: datetime) -> OAuthToken:
    """New access and refresh tokens on ``record``; the previous refresh hash kept."""
    access = tokens.GRANT_ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh = tokens.GRANT_REFRESH_PREFIX + secrets.token_urlsafe(32)
    record.token_hash = _hash(access)
    record.previous_refresh_hash = record.refresh_hash
    record.refresh_hash = _hash(refresh)
    record.access_expires_at = min(now + ACCESS_TTL, record.expires_at)
    return OAuthToken(
        access_token=access,
        token_type="Bearer",
        expires_in=max(1, int((record.access_expires_at - now).total_seconds())),
        refresh_token=refresh,
        scope=SCOPE,
    )


def _load_code(client_id: str, code: str) -> Optional[VigilCode]:
    with unit_of_work() as session:
        row = (
            session.query(OAuthPending)
            .filter(
                OAuthPending.code_hash == _hash(code),
                OAuthPending.client_id == client_id,
                OAuthPending.user_id.isnot(None),
            )
            .first()
        )
        if row is None:
            return None
        return VigilCode(
            code=code,
            scopes=row.scopes.split(),
            expires_at=row.expires_at.replace(tzinfo=timezone.utc).timestamp(),
            client_id=row.client_id,
            code_challenge=row.code_challenge,
            redirect_uri=AnyUrl(row.redirect_uri),
            redirect_uri_provided_explicitly=row.redirect_uri_explicit,
            resource=row.resource,
            subject=row.user_id,
        )


def _exchange_code(code: VigilCode) -> OAuthToken:
    now = utcnow()
    with unit_of_work() as session:
        # Locked and deleted: of two exchanges of one code, one finds nothing.
        row = (
            session.query(OAuthPending)
            .filter(OAuthPending.code_hash == _hash(code.code))
            .with_for_update()
            .first()
        )
        if row is None or row.expires_at <= now:
            raise _Refused("authorization code does not exist")
        # Shared locks on the person and the approving sign-in: a deactivation or
        # a revoke-everything (which ends sign-ins) either waits for this grant,
        # and so revokes it, or has finished, and so this refuses.
        user = (
            session.query(User)
            .filter(User.user_id == row.user_id)
            .with_for_update(read=True)
            .first()
        )
        signed_in = row.sign_in_id is None or (
            session.query(SignIn.sign_in_id)
            .filter(
                SignIn.sign_in_id == row.sign_in_id,
                SignIn.ended_at.is_(None),
            )
            .with_for_update(read=True)
            .first()
            is not None
        )
        client = session.get(OAuthClient, row.client_id)
        if user is None or not user.is_active or not signed_in or client is None:
            raise _Refused("authorization code does not exist")
        try:
            policy = token_policy.current(session)
        except token_policy.PolicyUnreadable as exc:
            raise _Refused(str(exc)) from exc
        record = McpCredential(
            credential_id=f"mcpc-{uuid.uuid4().hex[:16]}",
            user_id=user.user_id,
            label=client.name or "OAuth client",
            oauth_client_id=client.client_id,
            resource=row.resource,
            expires_at=now + min(tokens.lifetime(user, None, policy), GRANT_MAX),
        )
        issued = _issue(record, now)
        approved_sign_in = row.sign_in_id
        session.add(record)
        session.delete(row)
        client.last_used_at = now
        session.flush()
        trail_events.record(
            session,
            "oauth.grant_issued",
            actor=user.principal_id,
            principal=user.principal_id,
            credential_id=record.credential_id,
            client_id=client.client_id,
            expires_at=record.expires_at.isoformat(),
            sign_in=approved_sign_in,
        )
        return issued


def _refresh_reused(session, record: McpCredential, holder: User) -> list[int]:
    """The previous refresh token came back: someone holds a copy. The grant
    ends and the trail entry commits with it; the holder is told, high priority."""
    record.revoked_at = utcnow()
    trail_events.record(
        session,
        "oauth.refresh_reuse",
        actor=trail.SYSTEM,
        principal=holder.principal_id,
        credential_id=record.credential_id,
        client_id=record.oauth_client_id,
    )
    return token_notices.tell(
        holder,
        "App connection ended",
        f'The connection "{record.label}" was ended: an old refresh token was '
        "presented again, so a copy of it may be in someone else's hands. "
        "Connect the app again if you still use it.",
        "high",
        session=session,
    )


def _load_refresh(client_id: str, token: str) -> Optional[VigilRefreshToken]:
    if not token.startswith(tokens.GRANT_REFRESH_PREFIX):
        return None
    digest = _hash(token)
    notices: list[int] = []
    with unit_of_work() as session:
        record = (
            session.query(McpCredential)
            .filter(McpCredential.refresh_hash == digest)
            .first()
        )
        if record is None:
            reused = (
                session.query(McpCredential)
                .filter(
                    McpCredential.previous_refresh_hash == digest,
                    McpCredential.revoked_at.is_(None),
                )
                .with_for_update()
                .first()
            )
            if reused is not None:
                holder = session.get(User, reused.user_id)
                notices = _refresh_reused(session, reused, holder)
                logger.warning(
                    "OAuth grant %s ended: a used refresh token was presented again",
                    reused.credential_id,
                )
            found = None
        else:
            holder = session.get(User, record.user_id)
            found = (
                VigilRefreshToken(
                    token=token,
                    client_id=record.oauth_client_id,
                    scopes=[SCOPE],
                    expires_at=_epoch(record.expires_at),
                    resource=record.resource,
                    subject=record.user_id,
                    credential_id=record.credential_id,
                )
                if record.is_usable() and holder is not None and holder.is_active
                else None
            )
    if notices:
        from core.platform.notices import send_notices

        send_notices(notices)
    return found


def _exchange_refresh(refresh: VigilRefreshToken) -> OAuthToken:
    now = utcnow()
    with unit_of_work() as session:
        # Locked on the current hash: of two refreshes with one token, one wins.
        record = (
            session.query(McpCredential)
            .filter(
                McpCredential.credential_id == refresh.credential_id,
                McpCredential.refresh_hash == _hash(refresh.token),
            )
            .with_for_update()
            .first()
        )
        holder = session.get(User, record.user_id) if record else None
        if (
            record is None
            or not record.is_usable(now)
            or holder is None
            or not holder.is_active
        ):
            raise _Refused("refresh token does not exist")
        return _issue(record, now)


def _load_access(token: str) -> Optional[VigilAccessToken]:
    if not token.startswith(tokens.GRANT_ACCESS_PREFIX):
        return None
    now = utcnow()
    with unit_of_work() as session:
        record = (
            session.query(McpCredential)
            .filter(McpCredential.token_hash == _hash(token))
            .first()
        )
        if (
            record is None
            or not record.is_usable(now)
            or not record.access_expires_at
            or record.access_expires_at <= now
        ):
            return None
        return VigilAccessToken(
            token=token,
            client_id=record.oauth_client_id,
            scopes=[SCOPE],
            expires_at=_epoch(record.access_expires_at),
            resource=record.resource,
            subject=record.user_id,
            credential_id=record.credential_id,
        )


def _revoke(credential_id: str) -> None:
    """Either token ends the whole grant (RFC 7009 §2.1 allows it)."""
    if not tokens.revoke(credential_id):
        return
    with unit_of_work() as session:
        record = session.get(McpCredential, credential_id)
        holder = session.get(User, record.user_id)
        principal = holder.principal_id if holder else None
    trail_events.safety(
        "credential.revoked",
        actor=principal,
        principal=principal,
        credential_id=credential_id,
        reason="by the app",
    )


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
