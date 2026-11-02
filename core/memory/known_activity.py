"""Known Activity: what an analyst recorded as expected of one entity, why, and
until when.

It explains and never decides. People read it on the case and the alert, and
agents read it in their Recall as data. Nothing that moves an alert reads it, and
``.importlinter`` holds that: IP exclusions hide findings, this only explains them.

``active_for`` is the one resolver. Every read goes through it or through
``state_of``, the rule it applies.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from sqlalchemy import tuple_
from sqlalchemy.orm import Session

from core.auth import trail_events
from core.config import get_settings
from core.memory.entity_keys import entity_key, normalise_keys
from core.memory.recall_contract import ENTITY_KEY_TYPES
from core.storage.ip_address import normalize_ip
from core.storage.models.known_activity import KNOWN_ACTIVITY_ORIGINS, KnownActivity
from core.storage.schemas.base import _iso_utc
from core.time import utcnow

logger = logging.getLogger(__name__)

MAX_REASON_LENGTH = 2000
CREATED = "known_activity.create"
REMOVED = "known_activity.remove"

State = Literal["active", "expired", "removed"]


class KnownActivityError(ValueError):
    """A request the store refuses; the message is user-facing."""


def state_of(rule: KnownActivity, now: datetime) -> State:
    """A rule is in force until it expires, unless someone removes it first."""
    if rule.removed_at is not None:
        return "removed"
    return "active" if now < rule.expires_at else "expired"


def _key(rule: KnownActivity) -> str:
    return f"{rule.entity_kind}:{rule.entity_value}"


def active_for(
    entities: Iterable[str], now: datetime, rules: Iterable[KnownActivity]
) -> List[KnownActivity]:
    """The ``rules`` naming one of these Entity Keys and in force at ``now``."""
    wanted = set(normalise_keys(list(entities)))
    return [
        rule
        for rule in rules
        if _key(rule) in wanted and state_of(rule, now) == "active"
    ]


def _naive_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def read_active(
    session: Session, entities: Iterable[str], now: datetime
) -> List[KnownActivity]:
    """What ``active_for`` answers from the store, newest first."""
    keys = normalise_keys(list(entities))
    if not keys:
        return []
    pairs = [tuple(key.split(":", 1)) for key in keys]
    rows = (
        session.query(KnownActivity)
        .filter(
            tuple_(KnownActivity.entity_kind, KnownActivity.entity_value).in_(pairs),
            KnownActivity.removed_at.is_(None),
        )
        .order_by(KnownActivity.created_at.desc())
        .all()
    )
    return active_for(keys, _naive_utc(now), rows)


def serialize(rule: KnownActivity, now: datetime) -> Dict[str, Any]:
    return {
        "known_activity_id": rule.known_activity_id,
        "entity_kind": rule.entity_kind,
        "entity_value": rule.entity_value,
        "reason": rule.reason,
        "expires_at": _iso_utc(rule.expires_at),
        "created_by": rule.created_by,
        "created_at": _iso_utc(rule.created_at),
        "removed_at": _iso_utc(rule.removed_at) if rule.removed_at else None,
        "removed_by": rule.removed_by,
        "removal_reason": rule.removal_reason,
        "origin": rule.origin,
        "origin_ref": rule.origin_ref,
        "state": state_of(rule, now),
    }


def list_rules(
    session: Session, now: datetime, *, include_ended: bool = False
) -> List[KnownActivity]:
    """Rules in force, newest first; ``include_ended`` adds expired and removed."""
    rows = session.query(KnownActivity).order_by(KnownActivity.created_at.desc()).all()
    if include_ended:
        return rows
    return [row for row in rows if state_of(row, now) == "active"]


def default_expiry(now: datetime) -> datetime:
    return now + timedelta(days=get_settings().known_activity_default_days)


def _minted(entity_kind: str, entity_value: Any) -> Tuple[str, str]:
    """The kind and value as an Entity Key spells them, or a refusal."""
    if entity_kind not in ENTITY_KEY_TYPES:
        raise KnownActivityError(
            f"entity_kind must be one of {', '.join(ENTITY_KEY_TYPES)}"
        )
    value = str(entity_value or "")
    if entity_kind == "ip":
        canonical = normalize_ip(value)
        if canonical is None:
            raise KnownActivityError(f"{value!r} is not a single IPv4 or IPv6 address")
        value = canonical
    key = entity_key(entity_kind, value)
    if not key:
        raise KnownActivityError("An entity value is required")
    return entity_kind, key.split(":", 1)[1]


def _clean_reason(reason: Any, *, required: bool) -> Optional[str]:
    text = reason.strip() if isinstance(reason, str) else ""
    if not text:
        if required:
            raise KnownActivityError("A reason is required")
        return None
    if len(text) > MAX_REASON_LENGTH:
        raise KnownActivityError(
            f"Reason must be at most {MAX_REASON_LENGTH} characters"
        )
    return text


def _trail(
    session: Session,
    action: str,
    rule: KnownActivity,
    name: str,
    principal: Optional[str],
) -> None:
    # The kind is hashed; the value sits beside it, erasable, as a case IOC's does.
    trail_events.record(
        session,
        action,
        {"actor_text": name, "operator_text": rule.entity_value},
        actor=principal,
        object=rule.known_activity_id,
        entity_kind=rule.entity_kind,
        expires_at=_iso_utc(rule.expires_at),
        origin=rule.origin,
        origin_ref=rule.origin_ref,
        case_id=rule.origin_ref if rule.origin == "case" else None,
    )


def create_rule(
    session: Session,
    *,
    entity_kind: str,
    entity_value: Any,
    reason: Any,
    created_by: str,
    principal: Optional[str],
    expires_at: Optional[datetime] = None,
    origin: str = "ad_hoc",
    origin_ref: Optional[str] = None,
) -> KnownActivity:
    """Record one rule and its trail entry in the caller's transaction."""
    kind, value = _minted(entity_kind, entity_value)
    if origin not in KNOWN_ACTIVITY_ORIGINS:
        raise KnownActivityError(
            f"origin must be one of {', '.join(KNOWN_ACTIVITY_ORIGINS)}"
        )
    clean_reason = _clean_reason(reason, required=True)
    now = utcnow()
    until = _naive_utc(expires_at) if expires_at else default_expiry(now)
    if until <= now:
        raise KnownActivityError("The expiry must be in the future")
    rule = KnownActivity(
        known_activity_id=f"ka-{uuid.uuid4().hex[:16]}",
        entity_kind=kind,
        entity_value=value,
        reason=clean_reason,
        expires_at=until,
        created_by=created_by,
        created_at=now,
        origin=origin,
        origin_ref=str(origin_ref)[:100] if origin_ref else None,
    )
    session.add(rule)
    session.flush()
    _trail(session, CREATED, rule, created_by, principal)
    logger.info("Known activity %s added by %s", rule.known_activity_id, created_by)
    return rule


def remove_rule(
    session: Session,
    known_activity_id: str,
    *,
    removed_by: str,
    principal: Optional[str],
    reason: Any = None,
) -> Optional[KnownActivity]:
    """Mark a rule removed; ``None`` when there is no such rule.

    Removing it twice returns it unchanged, so a double click is not an error and
    the first removal's record is kept.
    """
    rule = session.get(KnownActivity, known_activity_id)
    if rule is None:
        return None
    if rule.removed_at is None:
        rule.removed_at = utcnow()
        rule.removed_by = removed_by
        rule.removal_reason = _clean_reason(reason, required=False)
        session.flush()
        _trail(session, REMOVED, rule, removed_by, principal)
        logger.info("Known activity %s removed by %s", known_activity_id, removed_by)
    return rule
