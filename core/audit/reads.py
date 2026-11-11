"""The trail as the Audit section reads it: one filter, one page (#2765).

``GET /api/v1/audit/entries`` and the ``list_audit_entries`` MCP tool both page
through here. Plain SELECTs: nothing here polls, confirms or carries, so the
post-restore pause and the erasure countdown never move on a read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from core.audit import feed, trail
from core.auth import principals

KINDS = frozenset(
    {
        principals.PERSON,
        principals.SERVICE_ACCOUNT,
        principals.AGENT,
        principals.SYSTEM,
    }
)

# What the Audit section shows of the feed. Not the host or the certificates,
# which stay on ``audit.forward``.
FEED_FIELDS = (
    "enabled",
    "behind",
    "last_delivered_at",
    "last_delivered_seq",
    "last_error",
    "unconfirmed",
    "paused",
)


def query(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    actor: Optional[str] = None,
    person: Optional[str] = None,
    kind: Optional[str] = None,
    area: Optional[str] = None,
    object_: Optional[str] = None,
    outcome: Optional[str] = None,
    attribution: Optional[str] = None,
    after: Optional[int] = None,
    sign_in: Optional[str] = None,
) -> trail.TrailQuery:
    """The shared filter. ``ValueError`` names what was refused."""
    kind_c = (kind or "").strip()
    if kind_c and kind_c not in KINDS:
        raise ValueError(f"unknown principal kind: {kind_c}")
    if after is not None and after < 0:
        raise ValueError("after must be 0 or more")
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
        sign_in=sign_in,
    )


def feed_health(session) -> dict:
    try:
        health = feed.health(session)
    except feed.FeedUnavailable:
        health = {"enabled": False, "behind": 0, "paused": False}
    return {key: health.get(key) for key in FEED_FIELDS}


def page(session, q: trail.TrailQuery, limit: int) -> dict:
    """Entries oldest first under ``q``, the tip, and the feed's health."""
    return {
        "entries": trail.read(session, q, limit),
        "tip": trail.current_tip(session).public(),
        "feed": feed_health(session),
    }
