"""Known activity (``/api/known-activity``).

Console-only, outside ``/api/v1``, like exclusions. Reading needs
``findings.read`` and writing needs ``findings.write``: the people who may work
the queue may explain what is in it, though a rule changes nothing in it.
"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from core.auth.current_user import get_current_user
from core.auth.permissions import permission_gate
from core.cases.linked_entities import finding_entity_summary
from core.memory.entity_keys import entity_context_candidates, entity_key
from core.memory.known_activity import (
    KnownActivityError,
    create_rule,
    default_expiry,
    list_rules,
    read_active,
    remove_rule,
    serialize,
)
from core.memory.recall_contract import ENTITY_KEY_TYPES
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import Case, Finding, User
from core.storage.models.base import case_findings
from core.storage.schemas.base import _iso_utc
from core.time import utcnow

router = APIRouter()
router.dependencies.append(permission_gate("findings.read"))

_WRITE = [permission_gate("findings.write")]

ROUTER_META = RouterMeta(
    prefix="/api/known-activity",
    tags=["known-activity"],
    auth=Auth.REQUIRED,
)

# What the linked-alert summary's groups are in the Entity Key vocabulary.
_SUMMARY_KINDS = {
    "hosts": "host",
    "users": "user",
    "ips": "ip",
    "domains": "domain",
    "urls": "url",
    "emails": "email",
    "hashes": "hash",
    "processes": "process",
}
_NAMED_PER_KIND = 50


class KnownActivityOut(BaseModel):
    known_activity_id: str
    entity_kind: str
    entity_value: str
    reason: str
    expires_at: str
    created_by: str
    created_at: str
    removed_at: Optional[str] = None
    removed_by: Optional[str] = None
    removal_reason: Optional[str] = None
    origin: str
    origin_ref: Optional[str] = None
    state: Literal["active", "expired", "removed"]


class KnownActivityListResponse(BaseModel):
    known_activity: List[KnownActivityOut]
    total: int
    default_expires_at: str = Field(
        ..., description="When a new rule ends if nobody picks a date (server config)."
    )
    entity_kinds: List[str]


class NamedEntity(BaseModel):
    entity_kind: str
    entity_value: str


class FindingContextResponse(BaseModel):
    known_activity: List[KnownActivityOut]


class CaseContextResponse(BaseModel):
    known_activity: List[KnownActivityOut]
    entities: List[NamedEntity]
    default_expires_at: str


class KnownActivityCreate(BaseModel):
    entity_kind: str = Field(..., max_length=20)
    entity_value: str = Field(..., max_length=1024)
    reason: str = Field(..., max_length=2000)
    expires_at: Optional[datetime] = Field(
        None, description="Absent takes the server's default duration."
    )
    origin: Literal["ad_hoc", "finding", "case", "run"] = "ad_hoc"
    origin_ref: Optional[str] = Field(None, max_length=100)


class KnownActivityRemove(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


def _person(user: User) -> str:
    """Who is acting, from the session. A person answers for a rule."""
    if user.service_account:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Known activity is recorded by a person",
        )
    return user.username or user.user_id


def _named(contexts: Iterable[Any]) -> Dict[str, Dict[str, str]]:
    """Every entity these alerts name, by Entity Key, once each."""
    named: Dict[str, Dict[str, str]] = {}
    for context in contexts:
        pairs = list(entity_context_candidates({"entity_context": context}))
        summary = finding_entity_summary(context, per_kind=_NAMED_PER_KIND, chars=1024)
        pairs += [
            (kind, value)
            for group, kind in _SUMMARY_KINDS.items()
            for value in summary.get(group, ())
        ]
        for kind, value in pairs:
            key = entity_key(kind, value)
            if key:
                named.setdefault(
                    key, {"entity_kind": kind, "entity_value": key.split(":", 1)[1]}
                )
    return named


@router.get("", response_model=KnownActivityListResponse)
def get_known_activity(session: UnitOfWorkSession, include_ended: bool = False):
    """Rules in force, newest first; ``include_ended`` adds expired and removed."""
    now = utcnow()
    rows = [
        serialize(rule, now)
        for rule in list_rules(session, now, include_ended=include_ended)
    ]
    return {
        "known_activity": rows,
        "total": len(rows),
        "default_expires_at": _iso_utc(default_expiry(now)),
        "entity_kinds": list(ENTITY_KEY_TYPES),
    }


@router.get("/for-finding/{finding_id}", response_model=FindingContextResponse)
def for_finding(finding_id: str, session: UnitOfWorkSession):
    """Rules in force for what this alert names. Reading them leaves it unchanged."""
    finding = session.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    now = utcnow()
    rules = read_active(session, _named([finding.entity_context]), now)
    return {"known_activity": [serialize(rule, now) for rule in rules]}


@router.get(
    "/for-case/{case_id}",
    response_model=CaseContextResponse,
    dependencies=[permission_gate("cases.read")],
)
def for_case(case_id: str, session: UnitOfWorkSession):
    """What this case's linked alerts name, and the rules in force for them."""
    if session.get(Case, case_id) is None:
        raise HTTPException(status_code=404, detail="Case not found")
    rows = (
        session.query(Finding.entity_context)
        .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
        .filter(case_findings.c.case_id == case_id)
        .all()
    )
    named = _named(context for (context,) in rows)
    now = utcnow()
    rules = read_active(session, named, now)
    return {
        "known_activity": [serialize(rule, now) for rule in rules],
        "entities": list(named.values()),
        "default_expires_at": _iso_utc(default_expiry(now)),
    }


@router.post(
    "",
    response_model=KnownActivityOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
def add_known_activity(
    body: KnownActivityCreate,
    session: UnitOfWorkSession,
    current_user: User = Depends(get_current_user),
):
    """Record one rule. The actor is the session's person, never the body."""
    try:
        rule = create_rule(
            session,
            entity_kind=body.entity_kind,
            entity_value=body.entity_value,
            reason=body.reason,
            expires_at=body.expires_at,
            origin=body.origin,
            origin_ref=body.origin_ref,
            created_by=_person(current_user),
            principal=current_user.principal_id,
        )
    except KnownActivityError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return serialize(rule, utcnow())


@router.post(
    "/{known_activity_id}/remove", response_model=KnownActivityOut, dependencies=_WRITE
)
def remove(
    known_activity_id: str,
    session: UnitOfWorkSession,
    body: Optional[KnownActivityRemove] = None,
    current_user: User = Depends(get_current_user),
):
    """Stop applying a rule. The row is kept, marked removed."""
    try:
        rule = remove_rule(
            session,
            known_activity_id,
            removed_by=_person(current_user),
            principal=current_user.principal_id,
            reason=body.reason if body else None,
        )
    except KnownActivityError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if rule is None:
        raise HTTPException(status_code=404, detail="Known activity not found")
    return serialize(rule, utcnow())
