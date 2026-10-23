"""Audit-feed setup: destination, a test send, and health (#2767).

``audit.forward`` and a person principal. A credential is refused first.
Saving goes through the integration config, which writes the trail entry.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.audit import feed
from core.auth.permissions import require_person_permission
from core.integrations import (  # noqa: F401  registers the config redactor
    integration_secrets,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import User

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/audit/feed",
    tags=["audit"],
    # The gate below is the session check. Router-level auth would answer 401
    # for a credential before that gate can answer 403.
    auth=Auth.ROUTER_MANAGED,
    reason=(
        "Feed setup refuses an MCP credential with 403 before the session "
        "dependency would turn it into a 401, and requires a person principal "
        "even in dev mode."
    ),
)

_FORWARD = require_person_permission("audit.forward")


class FeedIn(BaseModel):
    enabled: bool = False
    host: str = ""
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    # None keeps the stored secret. A blank string clears it.
    ca_pem: Optional[str] = None
    client_cert_pem: Optional[str] = None
    client_key_pem: Optional[str] = None


def _call(fn):
    try:
        return fn()
    except feed.FeedUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except feed.FeedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
def read_feed(session: UnitOfWorkSession, _: User = Depends(_FORWARD)):
    """Destination (without TLS material) and how far the cursor is behind the tip."""
    return _call(lambda: feed.health(session))


@router.put("")
def update_feed(body: FeedIn, user: User = Depends(_FORWARD)):
    """Save host, port, enabled and TLS material. One trail entry, from the config write."""

    def _save():
        return feed.save(
            user.user_id,
            enabled=body.enabled,
            host=body.host,
            port=body.port,
            ca_pem=body.ca_pem,
            client_cert_pem=body.client_cert_pem,
            client_key_pem=body.client_key_pem,
        )

    return _call(_save)


@router.post("/test")
def test_feed(session: UnitOfWorkSession, _: User = Depends(_FORWARD)):
    """Send one CEF test frame. Does not move the cursor or append a trail entry."""
    return _call(lambda: feed.send_test(session))
