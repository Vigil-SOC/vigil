"""Read, export, and poll the audit trail (#2765); set up its syslog feed (#2767).

One filter, from ``core.audit.trail``. List, export, and range verify need
``audit.read``. Poll needs ``audit.forward`` on a person sign-in: a missing
sign-in is refused before the role check, and a non-person is refused inside
``check_permission`` even in dev mode. A poll writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import ssl
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import core.integrations.integration_secrets  # noqa: F401 - the trail's redactor
from core.audit import feed, trail
from core.auth import current_user as current_user_module
from core.auth import principals
from core.auth.auth_service import AuthService
from core.auth.current_user import get_current_user
from core.auth.permissions import require_permission
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.secrets import (
    SECRET_WRITE_REFUSED_DETAIL,
    delete_secret,
    get_secret,
    secret_writes_refused,
    set_secret,
)
from core.storage.config_service import get_config_service
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/audit",
    tags=["audit"],
    auth=Auth.REQUIRED,
)

_READ = require_permission("audit.read")
_PEM_MAX = 64 * 1024
_SIGN_IN_REQUIRED = "A person sign-in is required"
_KINDS = frozenset(
    {
        principals.PERSON,
        principals.SERVICE_ACCOUNT,
        principals.AGENT,
        principals.SYSTEM,
    }
)


def _filters(
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    actor: Optional[str] = None,
    person: Optional[str] = None,
    kind: Optional[str] = None,
    area: Optional[str] = None,
    object_: Optional[str] = Query(None, alias="object"),
    outcome: Optional[str] = None,
    attribution: Optional[str] = None,
    after: Optional[int] = Query(None, ge=0),
) -> trail.TrailQuery:
    kind_c = (kind or "").strip()
    if kind_c and kind_c not in _KINDS:
        raise HTTPException(status_code=422, detail=f"unknown principal kind: {kind_c}")
    try:
        return trail.make_query(
            since=since,
            until=until,
            actor=actor,
            person=person,
            kind=kind,
            area=area,
            object_ref=object_,
            outcome=outcome,
            attribution=attribution,
            after=after,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _require_sign_in(request: Request) -> None:
    """A sid on the access token. Dev mode has no token; the kind check remains."""
    if current_user_module.DEV_MODE:
        return
    claims = getattr(request.state, "access_claims", None)
    if not claims or not claims.get("sid"):
        raise HTTPException(status_code=401, detail=_SIGN_IN_REQUIRED)


def require_forward():
    """Person sign-in, then the kind check, then ``audit.forward``."""

    def _check(request: Request, user: User = Depends(get_current_user)) -> User:
        _require_sign_in(request)
        if not AuthService.check_permission(user.user_id, "audit.forward"):
            raise HTTPException(
                status_code=403, detail="Permission denied: audit.forward required"
            )
        return user

    # The role x route matrix reads these off the dependency.
    _check.permission, _check.unsafe_only = "audit.forward", False
    return _check


def _ndjson(rows: list[dict], tip: trail.Tip):
    for row in rows:
        yield json.dumps(row, separators=(",", ":")) + "\n"
    yield json.dumps({"checkpoint": tip.public()}, separators=(",", ":")) + "\n"


@router.get("/entries")
def list_entries(
    session: UnitOfWorkSession,
    query: trail.TrailQuery = Depends(_filters),
    limit: int = Query(100, ge=1, le=trail.POLL_LIMIT),
    _: User = Depends(_READ),
):
    """The trail, oldest sequence first, under the shared filter."""
    return {
        "entries": trail.read(session, query, limit),
        "tip": trail.current_tip(session).public(),
    }


@router.get("/export")
def export_entries(
    session: UnitOfWorkSession,
    query: trail.TrailQuery = Depends(_filters),
    user: User = Depends(_READ),
):
    """NDJSON of the filter, closed by a checkpoint line. Writes one export entry."""
    if not user.principal_id:
        raise HTTPException(status_code=403, detail="A principal is required")
    rows, tip = trail.export(session, query, user.principal_id)
    return StreamingResponse(
        _ndjson(rows, tip),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit.ndjson"'},
    )


@router.get("/poll")
def poll_entries(
    session: UnitOfWorkSession,
    query: trail.TrailQuery = Depends(_filters),
    limit: int = Query(trail.POLL_LIMIT, ge=1, le=trail.POLL_LIMIT),
    _: User = Depends(require_forward()),
):
    """Entries after ``after``. A cursor past the tip is a reset, not an empty page."""
    if query.after is None:
        raise HTTPException(status_code=422, detail="poll needs after")
    page = trail.poll(session, query, limit)
    if page.reset:
        return {"reset": True, "tip": page.tip.public(), "restore": page.restore}
    return {"entries": list(page.entries), "tip": page.tip.public()}


@router.get("/verify")
def verify_range(
    session: UnitOfWorkSession,
    first: Optional[int] = Query(None, ge=0),
    last: Optional[int] = Query(None, ge=0),
    _: User = Depends(_READ),
):
    """``trail.verify`` over a sequence range. The first break, when there is one."""
    result = trail.verify(session, first, last)
    return {
        "first": result.first,
        "last": result.last,
        "first_bad": result.first_bad,
        "reason": result.reason,
        "ok": result.ok,
    }


# --- the syslog feed (#2767) ----------------------------------------------------


class FeedSettings(BaseModel):
    """Host, port and the switch; TLS material goes to the secrets store.

    An empty PEM keeps the stored one.
    """

    enabled: bool = False
    host: str = Field("", max_length=253)
    port: int = Field(6514, ge=1, le=65535)
    ca_pem: str = Field("", max_length=_PEM_MAX)
    client_cert_pem: str = Field("", max_length=_PEM_MAX)
    client_key_pem: str = Field("", max_length=_PEM_MAX)
    clear_client_cert: bool = False


def _stored(key: str) -> Optional[str]:
    return get_secret(key) or None


def _fingerprint(pem: Optional[str]) -> Optional[str]:
    return hashlib.sha256(pem.encode()).hexdigest() if pem else None


def _feed_view(session) -> dict:
    enabled, host, port = feed.settings(session)
    return {
        "enabled": enabled,
        "host": host,
        "port": port,
        "tls": {
            "ca": bool(_stored(feed.SECRET_CA)),
            "client_cert": bool(_stored(feed.SECRET_CLIENT_CERT)),
            "client_key": bool(_stored(feed.SECRET_CLIENT_KEY)),
        },
        "health": feed.health(session),
    }


@router.get("/feed")
def get_feed(session: UnitOfWorkSession, _: User = Depends(require_forward())):
    """The destination, which TLS material is stored, and the feed's health."""
    return _feed_view(session)


@router.put("/feed")
def set_feed(
    body: FeedSettings,
    session: UnitOfWorkSession,
    user: User = Depends(require_forward()),
):
    """Save the destination and the switch. The config write records the entry."""
    host = body.host.strip()
    if body.clear_client_cert and (body.client_cert_pem or body.client_key_pem):
        raise HTTPException(
            status_code=422, detail="Clear the client certificate or send a new one"
        )
    if body.enabled and not host:
        raise HTTPException(
            status_code=422, detail="A host is required to turn the feed on"
        )
    _, old_host, old_port = feed.settings(session)
    cert = (
        None
        if body.clear_client_cert
        else body.client_cert_pem or _stored(feed.SECRET_CLIENT_CERT)
    )
    key = (
        None
        if body.clear_client_cert
        else body.client_key_pem or _stored(feed.SECRET_CLIENT_KEY)
    )
    # A stored client key is never carried to a destination its owner did not choose.
    moved = (old_host, old_port) != (host, body.port)
    if (
        moved
        and _stored(feed.SECRET_CLIENT_KEY)
        and not body.client_key_pem
        and not body.clear_client_cert
    ):
        raise HTTPException(
            status_code=400,
            detail="The feed connects somewhere new; enter its client key again to save.",
        )
    try:
        feed.tls_context(
            feed.Destination(
                host or "-",
                body.port,
                body.ca_pem or _stored(feed.SECRET_CA),
                cert,
                key,
            )
        )
    except (ssl.SSLError, ValueError, OSError) as exc:
        raise HTTPException(
            status_code=422, detail=f"TLS material does not load: {exc}"
        ) from exc

    writes = {
        k: v
        for k, v in (
            (feed.SECRET_CA, body.ca_pem),
            (feed.SECRET_CLIENT_CERT, body.client_cert_pem),
            (feed.SECRET_CLIENT_KEY, body.client_key_pem),
        )
        if v
    }
    if writes and secret_writes_refused():
        raise HTTPException(status_code=500, detail=SECRET_WRITE_REFUSED_DETAIL)
    for name, value in writes.items():
        if not set_secret(name, value):
            raise HTTPException(status_code=500, detail=f"{name} was not stored")
    if body.clear_client_cert:
        delete_secret(feed.SECRET_CLIENT_CERT)
        delete_secret(feed.SECRET_CLIENT_KEY)

    saved = get_config_service(user_id=str(user.user_id)).set_integration_config(
        integration_id=feed.INTEGRATION_ID,
        # Fingerprints, not PEMs: a CA or certificate swap shows on the entry.
        config={
            "host": host or None,
            "port": body.port,
            "ca_sha256": _fingerprint(body.ca_pem or _stored(feed.SECRET_CA)),
            "client_cert_sha256": _fingerprint(cert),
        },
        enabled=body.enabled,
        integration_name=feed.INTEGRATION_NAME,
        integration_type="audit",
        change_reason="audit feed turned on" if body.enabled else "audit feed off",
    )
    if not saved:
        raise HTTPException(status_code=500, detail="The feed settings were not saved")
    session.expire_all()
    return _feed_view(session)


@router.post("/feed/test")
def test_feed(session: UnitOfWorkSession, _: User = Depends(require_forward())):
    """One test message to the saved destination. Not a trail entry; moves no cursor."""
    dest = feed.destination(session, when_off=True)
    if dest is None:
        raise HTTPException(status_code=422, detail="Save a host and port first")
    try:
        feed.send_test(dest)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:500]}
    return {"ok": True, "error": None}
