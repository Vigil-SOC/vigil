"""Directory sync's SCIM User and Group endpoints (#4423, #4424).

Mounted at ``/scim/v2``. Every handler answers 404 while sync is off and 403
for anyone but the directory-sync account. The body and the Authorization
header are never logged.

No bulk and no library: the handlers call the account and group functions.
A user payload that names groups writes the same member rows a Group push does,
and does not create a group that has not been pushed.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.audit.retention import ERASED_PREFIX, is_erased
from core.auth import (
    directory_groups,
    directory_sync,
    invites,
    principals,
    sso,
    sso_settings,
    trail_events,
)
from core.auth.auth_cookies import context_path_prefix
from core.auth.deactivation import clear_deactivation, deactivate_account
from core.auth.identity import (
    IN_USE,
    RESERVED,
    UNAVAILABLE,
    claim_sign_in_names,
    fold,
    identifier_in_use,
    sql_fold,
)
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import (
    DirectoryGroup,
    DirectoryPending,
    Principal,
    Role,
    SsoLink,
    User,
)
from core.teams import join_default
from core.time import utcnow
from services.api import group_mapping
from services.api.middleware.auth import get_current_active_user
from services.api.routers.users import _leaves_no_manager as leaves_no_manager

router = APIRouter()

ROUTER_META = RouterMeta(prefix="/scim/v2", tags=["scim"], auth=Auth.REQUIRED)

_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
_PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
_SP = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
_MEDIA = "application/scim+json"
# Both mean no value is kept or named: a reservation exists or can't be read.
_RESERVED = (RESERVED, UNAVAILABLE)
_EQ = re.compile(
    r"^\s*(userName|externalId|active)\s+eq\s+"
    r'(?:"((?:[^"\\]|\\.)*)"|(true|false))\s*$',
    re.IGNORECASE,
)
_GROUP_EQ = re.compile(
    r'^\s*(displayName|externalId)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$',
    re.IGNORECASE,
)
_MEMBER_EQ = re.compile(
    r'^members\[value eq "((?:[^"\\]|\\.)*)"\]$',
    re.IGNORECASE,
)
_GROUP_FILTER = re.compile(
    r'^groups\[value eq "((?:[^"\\]|\\.)*)"\]$',
    re.IGNORECASE,
)


class ScimFailure(Exception):
    def __init__(
        self,
        status: int,
        detail: str,
        scim_type: Optional[str] = None,
        rollback: bool = False,
    ):
        self.status, self.detail, self.scim_type = status, detail, scim_type
        # The request transaction must not keep a partial write. The trail entry
        # for it was committed on its own.
        self.rollback = rollback


def _fail(session: Session, exc: ScimFailure) -> JSONResponse:
    if exc.rollback:
        session.rollback()
    return _error(exc)


def _error(exc: ScimFailure) -> JSONResponse:
    body: dict = {
        "schemas": [_ERROR],
        "status": str(exc.status),
        "detail": exc.detail,
    }
    if exc.scim_type:
        body["scimType"] = exc.scim_type
    return JSONResponse(body, status_code=exc.status, media_type=_MEDIA)


def _json(body: dict, status: int = 200, etag: Optional[int] = None) -> JSONResponse:
    headers = {"ETag": f'"{etag}"'} if etag is not None else {}
    return JSONResponse(body, status_code=status, media_type=_MEDIA, headers=headers)


def _gate(session: Session, caller: User) -> sso_settings.SsoSettings:
    """404 while sync is off, whoever called. 403 for anyone but the sync account."""
    settings = sso_settings.read_row(session)[0].settings
    if not settings.sync_enabled:
        raise ScimFailure(404, "Directory sync is off")
    if not directory_sync.is_sync_account(caller):
        trail_events.refused(
            "directory.auth_failed",
            reason="not_sync_account",
            actor=caller.principal_id,
        )
        raise ScimFailure(403, "Directory sync is the only caller")
    if not settings.issuer:
        raise ScimFailure(409, "Single sign-on has no issuer")
    return settings


async def _payload(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ScimFailure(400, "The body is not JSON", "invalidValue") from None
    if not isinstance(data, dict):
        raise ScimFailure(400, "The body must be an object", "invalidValue")
    return data


def _external(data: dict, required: bool) -> Optional[str]:
    value = data.get("externalId")
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ScimFailure(400, "externalId is required", "invalidValue")
    value = value.strip()
    if len(value) > 1024:
        raise ScimFailure(400, "externalId is too long", "invalidValue")
    return value


def _work_email(data: dict) -> str:
    emails = data.get("emails")
    if not isinstance(emails, list):
        return ""
    work = primary = first = ""
    for item in emails:
        if isinstance(item, str):
            value, kind, is_primary = item, "", False
        elif isinstance(item, dict) and isinstance(item.get("value"), str):
            value = item["value"]
            kind = item.get("type") if isinstance(item.get("type"), str) else ""
            is_primary = item.get("primary") is True
        else:
            continue
        value = value.strip()
        if "@" not in value:
            continue
        first = first or value
        if is_primary:
            primary = value
        if kind == "work":
            work = value
    return work or primary or first


def _display(data: dict) -> Optional[str]:
    name = data.get("name")
    if isinstance(name, dict):
        formatted = name.get("formatted")
        if isinstance(formatted, str) and formatted.strip():
            return formatted.strip()[:200]
        parts = [
            part.strip()
            for part in (name.get("givenName"), name.get("familyName"))
            if isinstance(part, str) and part.strip()
        ]
        if parts:
            return " ".join(parts)[:200]
    display = data.get("displayName")
    if isinstance(display, str) and display.strip():
        return display.strip()[:200]
    return None


def _username(user_name: str) -> str:
    """The Vigil username for a SCIM ``userName``: the local part of an email.
    A reserved prefix gets ``sso-`` in front, as at first single sign-on, so a
    push can't make an account read as erased or as a Medic account."""
    username = user_name.split("@", 1)[0].strip() if "@" in user_name else user_name
    if username.lower().startswith(("medic-", ERASED_PREFIX)):
        username = f"sso-{username}"
    if not username or len(username) > 100:
        raise ScimFailure(400, "userName is not a usable username", "invalidValue")
    return username


def _names(user_name: str, data: dict) -> tuple[str, str, Optional[str]]:
    """Username, email and display name from a SCIM user.

    Email is the work email, or ``userName`` when that is an email. Username
    is ``userName`` when it is not an email, otherwise the local part. No suffix.
    """
    user_name = user_name.strip()
    email = _work_email(data) or (user_name if "@" in user_name else "")
    username = _username(user_name)
    email = email.strip()
    if "@" not in email or len(email) > 200:
        raise ScimFailure(400, "A work email is required", "invalidValue")
    return username, email, _display(data)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ScimFailure(400, "active must be true or false", "invalidValue")


def _match(session: Session, issuer: str, external_id: str) -> Optional[SsoLink]:
    rows = (
        session.query(SsoLink)
        .filter(
            SsoLink.issuer == issuer,
            or_(SsoLink.directory_id == external_id, SsoLink.subject == external_id),
        )
        .all()
    )
    rows.sort(key=lambda link: link.directory_id != external_id)
    return rows[0] if rows else None


def _person(session: Session, link: SsoLink) -> Optional[User]:
    user = (
        session.query(User)
        .filter(User.principal_id == link.principal_id)
        .with_for_update()
        .one_or_none()
    )
    principal = user and session.get(Principal, user.principal_id)
    if (
        user is None
        or principal is None
        or principal.deleted_at is not None
        or principal.kind != principals.PERSON
        or user.service_account
        or is_erased(user.username, user.principal_id)
    ):
        return None
    return user


def _skip(
    session: Session, caller: User, user: User, attribute: str, reason: str
) -> None:
    trail_events.record(
        session,
        "directory.attribute_skipped",
        actor=caller.principal_id,
        principal=user.principal_id,
        attribute=attribute,
        reason="reserved" if reason in _RESERVED else "in_use",
    )


def _apply_attributes(
    session: Session,
    caller: User,
    user: User,
    username: Optional[str],
    email: Optional[str],
    full_name: Optional[str],
) -> bool:
    """Apply a name and an email. A collision skips that attribute and is recorded."""
    changed = False
    # Exact comparison: a case-only change from the provider applies.
    if username and username != user.username:
        claim_sign_in_names(session, username)
        taken = identifier_in_use(session, username, except_user_id=user.user_id)
        if taken:
            _skip(session, caller, user, "username", taken)
        else:
            user.username = username
            changed = True
    if email and email != user.email:
        claim_sign_in_names(session, email)
        taken = identifier_in_use(session, email, except_user_id=user.user_id)
        if taken:
            _skip(session, caller, user, "email", taken)
        else:
            user.email = email
            user.is_verified = True
            invites.revoke_confirms(
                session, user.principal_id, caller.principal_id, "sso"
            )
            changed = True
    if full_name and full_name != user.full_name:
        user.full_name = full_name
        changed = True
    return changed


def _collide(
    session: Session, caller: User, external_id: str, username: str, email: str
) -> Optional[str]:
    """RESERVED when either name is reserved, IN_USE when an account or invite
    holds one, else None. Both are checked, so a reserved name refuses even when
    the other belongs to a live account. A reserved refusal records the
    directory id and the reason only."""
    claim_sign_in_names(session, username, email)
    found = {
        attr: identifier_in_use(session, value)
        for attr, value in (("username", username), ("email", email))
    }
    if any(v in _RESERVED for v in found.values()):
        trail_events.record(
            session,
            "directory.collision",
            actor=caller.principal_id,
            external_id=external_id,
            reason="reserved",
        )
        return RESERVED
    hit = [attr for attr, v in found.items() if v]
    if not hit:
        return None
    trail_events.record(
        session,
        "directory.collision",
        actor=caller.principal_id,
        external_id=external_id,
        reason="in_use",
        attributes=hit,
    )
    return IN_USE


def _would_leave_no_manager(session: Session, user: User) -> bool:
    return leaves_no_manager(session, users={user.user_id: (user.role_id, False)})


def _deactivate(session: Session, caller: User, user: User) -> None:
    """Deactivate with route ``sync``. The last role manager stays active."""
    if not user.is_active:
        return None
    if _would_leave_no_manager(session, user):
        # Its own transaction: the request rolls back, and the status still lists it.
        trail_events.refused(
            "directory.last_manager",
            actor=caller.principal_id,
            principal=user.principal_id,
            user_id=user.user_id,
            username=user.username,
        )
        raise ScimFailure(
            409,
            "This would leave no person holding roles.manage who can sign in",
            rollback=True,
        )
    deactivate_account(
        session, user, actor=caller.principal_id, by_person=False, route="sync"
    )
    return None


def _reactivate(session: Session, caller: User, user: User) -> bool:
    """True when this call made the account active. Only a sync route comes back."""
    if user.is_active:
        return False
    if user.deactivation_route != "sync":
        trail_events.record(
            session,
            "directory.reactivate_refused",
            actor=caller.principal_id,
            principal=user.principal_id,
            deactivation_route=user.deactivation_route,
        )
        return False
    user.is_active = True
    clear_deactivation(user)
    user.dormancy_floor = utcnow()
    trail_events.record(
        session,
        "user.enabled",
        actor=caller.principal_id,
        principal=user.principal_id,
        via="scim",
    )
    return True


def _touch(user: User) -> None:
    user.scim_version = (user.scim_version or 1) + 1


def _pushed(
    session: Session,
    caller: User,
    external_id: Optional[str],
    user: User,
    op: str,
    active: Optional[bool] = None,
) -> None:
    """``active`` true when the push set ``active: true``: it settles a refused deactivation."""
    trail_events.record(
        session,
        "directory.pushed",
        actor=caller.principal_id,
        principal=user.principal_id,
        external_id=external_id,
        op=op,
        active=True if active is True else None,
    )


def _version_of(header: Optional[str]) -> Optional[int]:
    """None: no If-Match, so the write applies in arrival order. ``*`` is the same."""
    if header is None:
        return None
    raw = header.strip()
    if not raw or raw == "*":
        return None
    try:
        return int(raw.removeprefix("W/").strip().strip('"'))
    except ValueError:
        raise ScimFailure(400, "If-Match must be the user's version") from None


def _check_version(user: User, header: Optional[str]) -> None:
    _check_match(user.scim_version or 1, header, "user")


def _check_match(current: int, header: Optional[str], noun: str) -> None:
    version = _version_of(header)
    if version is not None and version != current:
        raise ScimFailure(412, f"The {noun} has changed since it was read")


def _returned_user_name(user: User) -> str:
    local, _, domain = user.email.partition("@")
    if domain and fold(local) == fold(user.username):
        return user.email
    return user.username


def _representation(request: Request, user: User, link: SsoLink) -> dict:
    external = link.directory_id or link.subject
    user_name = _returned_user_name(user)
    location = (
        f"{str(request.base_url).rstrip('/')}"
        f"{context_path_prefix()}/scim/v2/Users/{quote(user.user_id)}"
    )
    return {
        "schemas": [_USER],
        "id": user.user_id,
        "externalId": external,
        "userName": user_name,
        "name": {"formatted": user.full_name},
        "displayName": user.full_name,
        "emails": [{"value": user.email, "type": "work", "primary": True}],
        "active": bool(user.is_active),
        "meta": {
            "resourceType": "User",
            "location": location,
            "version": str(user.scim_version or 1),
        },
    }


def _visible(session: Session, issuer: str):
    """Managed, active accounts at this issuer. A deactivated one is absent."""
    return (
        session.query(User, SsoLink)
        .join(SsoLink, SsoLink.principal_id == User.principal_id)
        .join(Principal, Principal.principal_id == User.principal_id)
        .filter(
            SsoLink.issuer == issuer,
            SsoLink.managed.is_(True),
            User.is_active.is_(True),
            Principal.deleted_at.is_(None),
        )
    )


def _managed(
    session: Session, issuer: str, user_id: str
) -> Optional[tuple[User, SsoLink]]:
    user = (
        session.query(User)
        .filter(User.user_id == user_id)
        .with_for_update()
        .one_or_none()
    )
    if user is None:
        return None
    principal = session.get(Principal, user.principal_id)
    if principal is None or principal.deleted_at is not None:
        return None
    link = (
        session.query(SsoLink)
        .filter(
            SsoLink.principal_id == user.principal_id,
            SsoLink.issuer == issuer,
            SsoLink.managed.is_(True),
        )
        .first()
    )
    if link is None:
        return None
    return user, link


def _filter(query, expression: Optional[str]):
    if expression is None or not expression.strip():
        return query
    matched = _EQ.match(expression)
    if matched is None:
        raise ScimFailure(
            400,
            "Only eq on userName, externalId and active is supported",
            "invalidFilter",
        )
    attr, quoted, bare = matched.group(1), matched.group(2), matched.group(3)
    value = (quoted if quoted is not None else bare).replace('\\"', '"')
    key = attr.lower()
    if key == "active":
        if value.lower() != "true":
            return query.filter(User.user_id.is_(None))
        return query
    if key == "username":
        folded = fold(value)
        return query.filter(
            or_(sql_fold(User.username) == folded, sql_fold(User.email) == folded)
        )
    return query.filter(
        or_(
            SsoLink.directory_id == value,
            SsoLink.subject == value,
        )
    )


def _take_over(session: Session, caller: User, link: SsoLink, external_id: str) -> User:
    """A push whose externalId matches a link manages that account."""
    user = _person(session, link)
    if user is None or user.break_glass_at is not None:
        trail_events.record(
            session,
            "directory.takeover_refused",
            actor=caller.principal_id,
            principal=link.principal_id,
            external_id=external_id,
        )
        raise ScimFailure(409, "That account can't be taken over")
    if link.directory_id is None:
        other = sso.linked_by_directory(session, link.issuer, external_id)
        if other is None or other.link_id == link.link_id:
            link.directory_id = external_id
    elif link.directory_id != external_id:
        trail_events.record(
            session,
            "directory.claim_ignored",
            actor=caller.principal_id,
            principal=link.principal_id,
            directory_id=external_id,
        )
    link.managed = True
    return user


def _hold(
    session: Session,
    external_id: str,
    username: str,
    email: str,
    full_name: Optional[str],
) -> None:
    """Write or refresh the pending push for ``external_id``."""
    values = dict(
        username=username, email=email, display_name=full_name, received_at=utcnow()
    )
    session.execute(
        insert(DirectoryPending)
        .values(
            pending_id=f"pending-{uuid.uuid4().hex[:12]}",
            external_id=external_id,
            **values,
        )
        .on_conflict_do_update(index_elements=["external_id"], set_=values)
    )


def _create(
    session: Session,
    caller: User,
    settings: sso_settings.SsoSettings,
    data: dict,
    external_id: str,
) -> User:
    user_name = data.get("userName")
    if not isinstance(user_name, str) or not user_name.strip():
        raise ScimFailure(400, "userName is required", "invalidValue")
    username, email, full_name = _names(user_name, data)
    role = session.get(Role, settings.default_role_id)
    if role is None:
        raise ScimFailure(409, "The default role no longer exists")
    # An invite for this address is the same person joining. Cancel it instead
    # of refusing the push — but only when no account already holds the name,
    # so a real collision leaves the invite waiting (#4441).
    claim_sign_in_names(session, username, email)
    held_by_account = identifier_in_use(
        session, username, pending_invites=False
    ) or identifier_in_use(session, email, pending_invites=False)
    if not held_by_account:
        invites.cancel_pending(session, email, "directory")
    elif collided := _collide(session, caller, external_id, username, email):
        # Kept for an admin to link: the failure commits, as the trail entry does.
        # A reserved name is never stored.
        if collided != RESERVED:
            _hold(session, external_id, username, email, full_name)
        raise ScimFailure(
            409, "That username or email is in use or reserved", "uniqueness"
        )
    user = User(
        user_id=f"user-{uuid.uuid4().hex[:12]}",
        username=username,
        email=email,
        password_hash="",
        full_name=full_name or username,
        role_id=role.role_id,
        is_active=True,
        is_verified=True,
        service_account=False,
        login_count=0,
    )
    principals.add_for_user(session, user)
    session.add(user)
    session.flush()
    join_default(session, user.principal_id, actor=caller.principal_id)
    session.add(
        SsoLink(
            link_id=f"sso-{uuid.uuid4().hex}",
            principal_id=user.principal_id,
            issuer=settings.issuer,
            subject=external_id,
            directory_id=external_id,
            managed=True,
        )
    )
    session.flush()
    trail_events.record(
        session,
        "user.created",
        {"personal": trail_events.personal(user)},
        actor=caller.principal_id,
        principal=user.principal_id,
        role_id=user.role_id,
        via="scim",
        external_id=external_id,
    )
    return user


def _active_of(data: dict, default: Optional[bool]) -> Optional[bool]:
    if "active" not in data:
        return default
    return _as_bool(data.get("active"))


def _ops(data: dict) -> dict:
    """A PATCH body as the fields it sets. Unknown attributes are ignored."""
    ops = data.get("Operations", data.get("operations"))
    if not isinstance(ops, list):
        raise ScimFailure(400, "Operations must be a list", "invalidValue")
    fields: dict = {}
    for op in ops:
        if not isinstance(op, dict):
            raise ScimFailure(400, "Each operation must be an object", "invalidValue")
        name = str(op.get("op") or "").lower()
        if name not in ("add", "replace", "remove"):
            raise ScimFailure(
                400, "Only add, remove and replace are supported", "invalidValue"
            )
        path = op.get("path")
        value = op.get("value")
        if name == "remove":
            _op_remove_groups(fields, path, value)
            continue
        if path is None and isinstance(value, dict):
            piece = _resource_fields(value)
            ops = piece.pop("groups_ops", None)
            fields.update(piece)
            if ops:
                fields.setdefault("groups_ops", []).extend(ops)
            continue
        if not isinstance(path, str):
            raise ScimFailure(400, "An operation needs a path", "invalidValue")
        short = path.split(":")[-1]
        filtered = _GROUP_FILTER.match(short)
        if filtered:
            _op_groups(fields, name, [filtered.group(1).replace('\\"', '"')])
            continue
        if short.lower() == "groups":
            _op_groups(fields, name, _group_refs(value))
            continue
        if short == "active":
            fields["active"] = _as_bool(value)
        elif short in ("userName", "displayName"):
            fields[short] = value if isinstance(value, str) else ""
        elif short in ("name.formatted", "formatted"):
            fields["displayName"] = value if isinstance(value, str) else ""
        elif short == "name":
            fields["displayName"] = (
                value.get("formatted") if isinstance(value, dict) else value
            )
        elif short.startswith("emails"):
            fields["emails"] = value if isinstance(value, list) else [{"value": value}]
        elif short == "externalId" and isinstance(value, str):
            fields["externalId"] = value.strip()
    return fields


def _group_refs(value) -> list[str]:
    """Group ids from a SCIM groups value: strings, or objects with ``value``."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        raw = value.get("value")
        return [raw.strip()] if isinstance(raw, str) and raw.strip() else []
    if not isinstance(value, list):
        return []
    found = []
    for item in value:
        found.extend(_group_refs(item))
    return found


def _push_groups(fields: dict, kind: str, ids: list[str]) -> None:
    fields.setdefault("groups_ops", []).append((kind, list(ids)))


def _op_groups(fields: dict, name: str, ids: list[str]) -> None:
    _push_groups(fields, "add" if name == "add" else "replace", ids)


def _op_remove_groups(fields: dict, path, value) -> None:
    """A remove that is not about groups is still refused, as before."""
    if not isinstance(path, str):
        raise ScimFailure(400, "Only add and replace are supported", "invalidValue")
    short = path.split(":")[-1]
    filtered = _GROUP_FILTER.match(short)
    if filtered:
        _push_groups(fields, "remove", [filtered.group(1).replace('\\"', '"')])
        return
    if short.lower() != "groups":
        raise ScimFailure(400, "Only add and replace are supported", "invalidValue")
    ids = _group_refs(value)
    if ids:
        _push_groups(fields, "remove", ids)
    else:
        # Removing the attribute with no value clears the person's groups.
        _push_groups(fields, "replace", [])


def _resource_fields(data: dict) -> dict:
    fields = {}
    if isinstance(data.get("userName"), str):
        fields["userName"] = data["userName"]
    if isinstance(data.get("displayName"), str):
        fields["displayName"] = data["displayName"]
    display = _display(data)
    if display:
        fields["displayName"] = display
    email = _work_email(data)
    if email:
        fields["email"] = email
    if "active" in data:
        fields["active"] = _as_bool(data.get("active"))
    if isinstance(data.get("externalId"), str):
        fields["externalId"] = data["externalId"].strip()
    if "groups" in data:
        fields["groups_ops"] = [("replace", _group_refs(data.get("groups")))]
    return fields


def _fields_to_names(
    user: User, fields: dict
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Username, email and display name a patch wants, or None to leave each."""
    username = email = None
    if isinstance(fields.get("userName"), str) and fields["userName"].strip():
        user_name = fields["userName"].strip()
        email = user_name if "@" in user_name else None
        username = _username(user_name)
    if isinstance(fields.get("email"), str) and "@" in fields["email"]:
        email = fields["email"].strip()
    if isinstance(fields.get("emails"), list):
        found = _work_email({"emails": fields["emails"]})
        if found:
            email = found
    if email and len(email) > 200:
        raise ScimFailure(400, "A work email is required", "invalidValue")
    full_name = fields.get("displayName")
    if isinstance(full_name, str):
        full_name = full_name.strip()[:200] or None
    else:
        full_name = None
    return username, email, full_name


def _sync_user_groups(
    session: Session,
    caller: User,
    user: User,
    ops: list[tuple[str, list[str]]],
) -> bool:
    """Write group ids from a user payload, in order, then apply. True if membership moved."""
    if not ops:
        return False
    changed = False
    for kind, ids in ops:
        unknown, moved = directory_groups.apply_user_groups(
            session,
            user.principal_id,
            add=ids if kind == "add" else [],
            remove=ids if kind == "remove" else [],
            replace=ids if kind == "replace" else None,
        )
        for key in unknown:
            trail_events.record(
                session,
                "directory.group_unknown",
                actor=caller.principal_id,
                principal=user.principal_id,
                group=key,
            )
        changed = changed or moved
    if changed:
        _apply_members(session, {user.principal_id})
    return changed


def _apply_members(session: Session, principals: set[str]) -> None:
    """Role and team changes for these principals, when a mapping author is saved."""
    if not principals:
        return
    stored = sso_settings.read_row(session)[0]
    group_mapping.apply_current(session, stored.settings, principals, stored.version)


# --- discovery -----------------------------------------------------------------


def _provider_config() -> dict:
    return {
        "schemas": [_SP],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": True},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer",
                "description": "The directory sync token from Vigil's single sign-on settings.",
            }
        ],
    }


def _user_schema() -> dict:
    return {
        "id": _USER,
        "name": "User",
        "description": "A person pushed from the identity provider.",
        "attributes": [
            {
                "name": "userName",
                "type": "string",
                "required": True,
                "uniqueness": "server",
            },
            {"name": "externalId", "type": "string", "required": True},
            {"name": "name", "type": "complex"},
            {"name": "emails", "type": "complex", "multiValued": True},
            {"name": "active", "type": "boolean"},
        ],
    }


def _user_type(request: Request) -> dict:
    location = f"{str(request.base_url).rstrip('/')}{context_path_prefix()}/scim/v2/ResourceTypes/User"
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "schema": _USER,
        "meta": {"resourceType": "ResourceType", "location": location},
    }


def _group_schema() -> dict:
    return {
        "id": _GROUP,
        "name": "Group",
        "description": "A group pushed from the identity provider.",
        "attributes": [
            {"name": "displayName", "type": "string", "required": True},
            {"name": "externalId", "type": "string", "required": True},
            {"name": "members", "type": "complex", "multiValued": True},
        ],
    }


def _group_type(request: Request) -> dict:
    location = (
        f"{str(request.base_url).rstrip('/')}"
        f"{context_path_prefix()}/scim/v2/ResourceTypes/Group"
    )
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "Group",
        "name": "Group",
        "endpoint": "/Groups",
        "schema": _GROUP,
        "meta": {"resourceType": "ResourceType", "location": location},
    }


@router.get("/ServiceProviderConfig")
def service_provider_config(
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        _gate(session, caller)
        return _json(_provider_config())
    except ScimFailure as exc:
        return _fail(session, exc)


@router.get("/ResourceTypes")
def resource_types(
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        _gate(session, caller)
        return _json(
            {
                "schemas": [_LIST],
                "totalResults": 2,
                "Resources": [_user_type(request), _group_type(request)],
            }
        )
    except ScimFailure as exc:
        return _fail(session, exc)


@router.get("/ResourceTypes/User")
def resource_type_user(
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        _gate(session, caller)
        return _json(_user_type(request))
    except ScimFailure as exc:
        return _fail(session, exc)


@router.get("/ResourceTypes/Group")
def resource_type_group(
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        _gate(session, caller)
        return _json(_group_type(request))
    except ScimFailure as exc:
        return _fail(session, exc)


@router.get("/Schemas")
def schemas(
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        _gate(session, caller)
        return _json(
            {
                "schemas": [_LIST],
                "totalResults": 2,
                "Resources": [_user_schema(), _group_schema()],
            }
        )
    except ScimFailure as exc:
        return _fail(session, exc)


@router.get("/Schemas/{schema_id:path}")
def schema_one(
    schema_id: str,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        _gate(session, caller)
        if schema_id == _USER:
            return _json(_user_schema())
        if schema_id == _GROUP:
            return _json(_group_schema())
        raise ScimFailure(404, "No such schema")
    except ScimFailure as exc:
        return _fail(session, exc)


# --- Users ---------------------------------------------------------------------


@router.get("/Users")
def list_users(
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
    filter: Optional[str] = Query(None, alias="filter"),
    startIndex: int = Query(1, ge=1),
    count: int = Query(100, ge=0, le=200),
):
    try:
        settings = _gate(session, caller)
        query = _filter(_visible(session, settings.issuer), filter)
        total = query.count()
        rows = (
            []
            if count == 0
            else query.order_by(User.username).offset(startIndex - 1).limit(count).all()
        )
        return _json(
            {
                "schemas": [_LIST],
                "totalResults": total,
                "startIndex": startIndex,
                "itemsPerPage": len(rows),
                "Resources": [
                    _representation(request, user, link) for user, link in rows
                ],
            }
        )
    except ScimFailure as exc:
        return _fail(session, exc)


@router.post("/Users", status_code=201)
async def create_user(
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        settings = _gate(session, caller)
        data = await _payload(request)
        external_id = _external(data, required=True)
        link = _match(session, settings.issuer, external_id)
        created = link is None
        attrs = False
        if link is None:
            user = _create(session, caller, settings, data, external_id)
            link = (
                session.query(SsoLink)
                .filter(SsoLink.principal_id == user.principal_id)
                .one()
            )
        else:
            user = _take_over(session, caller, link, external_id)
            username, email, full_name = (None, None, None)
            user_name = data.get("userName")
            if isinstance(user_name, str) and user_name.strip():
                username, email, full_name = _names(user_name, data)
            else:
                full_name = _display(data)
                email = _work_email(data) or None
            attrs = _apply_attributes(session, caller, user, username, email, full_name)
        # Created or taken over: nothing waits for a link under this externalId.
        session.query(DirectoryPending).filter(
            DirectoryPending.external_id == external_id
        ).delete(synchronize_session=False)
        active = _active_of(data, True)
        state = False
        if active is False:
            was_active = user.is_active
            _deactivate(session, caller, user)
            state = was_active and not user.is_active
        elif active is True:
            state = _reactivate(session, caller, user)
        # A new row is version 1. A takeover bumps when a name or the active flag moved.
        if not created and (attrs or state):
            _touch(user)
        if "groups" in data:
            _sync_user_groups(
                session,
                caller,
                user,
                [("replace", _group_refs(data.get("groups")))],
            )
        _pushed(
            session,
            caller,
            external_id,
            user,
            "create",
            active="active" in data and active is True,
        )
        return _json(
            _representation(request, user, link),
            201 if created else 200,
            user.scim_version,
        )
    except ScimFailure as exc:
        return _fail(session, exc)


@router.get("/Users/{user_id}")
def get_user(
    user_id: str,
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        settings = _gate(session, caller)
        row = _visible(session, settings.issuer).filter(User.user_id == user_id).first()
        if row is None:
            raise ScimFailure(404, "User not found")
        user, link = row
        return _json(_representation(request, user, link), etag=user.scim_version)
    except ScimFailure as exc:
        return _fail(session, exc)


@router.patch("/Users/{user_id}")
async def patch_user(
    user_id: str,
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    try:
        settings = _gate(session, caller)
        found = _managed(session, settings.issuer, user_id)
        if found is None:
            raise ScimFailure(404, "User not found")
        user, link = found
        _check_version(user, if_match)
        fields = _ops(await _payload(request))
        if isinstance(fields.get("externalId"), str) and fields["externalId"] not in (
            link.directory_id,
            "",
        ):
            trail_events.record(
                session,
                "directory.claim_ignored",
                actor=caller.principal_id,
                principal=user.principal_id,
                directory_id=fields["externalId"],
            )
        username, email, full_name = _fields_to_names(user, fields)
        changed = _apply_attributes(session, caller, user, username, email, full_name)
        if "active" in fields:
            if fields["active"] is False:
                was_active = user.is_active
                _deactivate(session, caller, user)
                changed = changed or (was_active and not user.is_active)
            elif _reactivate(session, caller, user):
                changed = True
        groups_changed = _sync_user_groups(
            session, caller, user, fields.get("groups_ops") or []
        )
        if changed or groups_changed:
            _touch(user)
        _pushed(session, caller, link.directory_id, user, "patch", fields.get("active"))
        return _json(_representation(request, user, link), etag=user.scim_version)
    except ScimFailure as exc:
        return _fail(session, exc)


@router.delete("/Users/{user_id}", status_code=204)
def delete_user(
    user_id: str,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    try:
        settings = _gate(session, caller)
        found = _managed(session, settings.issuer, user_id)
        if found is None:
            raise ScimFailure(404, "User not found")
        user, link = found
        _check_version(user, if_match)
        was_active = user.is_active
        _deactivate(session, caller, user)
        if was_active and not user.is_active:
            _touch(user)
        _pushed(session, caller, link.directory_id, user, "delete")
        return Response(status_code=204)
    except ScimFailure as exc:
        return _fail(session, exc)


# --- Groups --------------------------------------------------------------------


def _group_location(request: Request, group_id: str) -> str:
    return (
        f"{str(request.base_url).rstrip('/')}"
        f"{context_path_prefix()}/scim/v2/Groups/{quote(group_id)}"
    )


def _group_body(request: Request, session: Session, group: DirectoryGroup) -> dict:
    members = [
        {
            "value": user.user_id,
            "display": (user.full_name or "").strip() or user.username,
        }
        for user in directory_groups.member_users(session, group.group_id)
    ]
    return {
        "schemas": [_GROUP],
        "id": group.group_id,
        "externalId": group.external_id,
        "displayName": group.display_name,
        "members": members,
        "meta": {
            "resourceType": "Group",
            "location": _group_location(request, group.group_id),
            "version": str(group.version or 1),
        },
    }


def _group_filter(query, expression: Optional[str]):
    if expression is None or not expression.strip():
        return query
    matched = _GROUP_EQ.match(expression)
    if matched is None:
        raise ScimFailure(
            400,
            "Only eq on displayName and externalId is supported",
            "invalidFilter",
        )
    attr, quoted = matched.group(1).lower(), matched.group(2).replace('\\"', '"')
    column = (
        DirectoryGroup.display_name
        if attr == "displayname"
        else DirectoryGroup.external_id
    )
    return query.filter(column == quoted)


def _label(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScimFailure(400, "displayName is required", "invalidValue")
    value = value.strip()
    if len(value) > directory_groups.DISPLAY_MAX:
        raise ScimFailure(400, "displayName is too long", "invalidValue")
    return value


def _resolved(session: Session, settings, caller: User, values: list[str]) -> set[str]:
    """Principal ids of accounts that already exist. The rest are recorded and skipped."""
    found: set[str] = set()
    for value in values:
        user = directory_groups.account(session, settings.issuer, value)
        if user is None:
            trail_events.record(
                session,
                "directory.member_skipped",
                actor=caller.principal_id,
                value=value,
            )
            continue
        found.add(user.principal_id)
    return found


def _group_patch(data: dict) -> list[tuple]:
    """Operations as (kind, payload). Unknown attributes are ignored."""
    ops = data.get("Operations", data.get("operations"))
    if not isinstance(ops, list):
        raise ScimFailure(400, "Operations must be a list", "invalidValue")
    parsed = []
    for op in ops:
        if not isinstance(op, dict):
            raise ScimFailure(400, "Each operation must be an object", "invalidValue")
        name = str(op.get("op") or "").lower()
        if name not in ("add", "replace", "remove"):
            raise ScimFailure(
                400, "Only add, remove and replace are supported", "invalidValue"
            )
        path = op.get("path")
        value = op.get("value")
        if path is None and isinstance(value, dict):
            if isinstance(value.get("displayName"), str):
                parsed.append(("display", value["displayName"]))
            if isinstance(value.get("externalId"), str):
                parsed.append(("external", value["externalId"].strip()))
            if "members" in value:
                parsed.append(("replace", _group_refs(value.get("members"))))
            continue
        if not isinstance(path, str):
            raise ScimFailure(400, "An operation needs a path", "invalidValue")
        short = path.split(":")[-1]
        member = _MEMBER_EQ.match(short)
        if member:
            one = [member.group(1).replace('\\"', '"')]
            parsed.append(("remove" if name == "remove" else "add", one))
            continue
        if short.lower() == "members":
            ids = _group_refs(value)
            if name == "replace" or (name == "remove" and not ids):
                parsed.append(("replace", ids))
            elif name == "remove":
                parsed.append(("remove", ids))
            else:
                parsed.append(("add", ids))
            continue
        if short.lower() == "displayname":
            parsed.append(("display", value if isinstance(value, str) else ""))
        elif short.lower() == "externalid" and isinstance(value, str):
            parsed.append(("external", value.strip()))
    return parsed


def _pushed_group(
    session: Session, caller: User, group: DirectoryGroup, op: str
) -> None:
    trail_events.record(
        session,
        "directory.pushed",
        actor=caller.principal_id,
        group_id=group.group_id,
        external_id=group.external_id,
        op=op,
    )


def _require_external(session: Session, caller: User, data: dict) -> str:
    value = data.get("externalId")
    if not isinstance(value, str) or not value.strip():
        trail_events.record(
            session,
            "directory.group_refused",
            actor=caller.principal_id,
            reason="external_id",
        )
    return _external(data, required=True)


@router.get("/Groups")
def list_groups(
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
    filter: Optional[str] = Query(None, alias="filter"),
    startIndex: int = Query(1, ge=1),
    count: int = Query(100, ge=0, le=200),
):
    try:
        _gate(session, caller)
        query = _group_filter(session.query(DirectoryGroup), filter)
        total = query.count()
        rows = (
            []
            if count == 0
            else query.order_by(DirectoryGroup.display_name, DirectoryGroup.group_id)
            .offset(startIndex - 1)
            .limit(count)
            .all()
        )
        return _json(
            {
                "schemas": [_LIST],
                "totalResults": total,
                "startIndex": startIndex,
                "itemsPerPage": len(rows),
                "Resources": [_group_body(request, session, group) for group in rows],
            }
        )
    except ScimFailure as exc:
        return _fail(session, exc)


@router.post("/Groups", status_code=201)
async def create_group(
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        settings = _gate(session, caller)
        data = await _payload(request)
        external_id = _require_external(session, caller, data)
        if directory_groups.by_external(session, external_id) is not None:
            raise ScimFailure(
                409, "A group with that externalId already exists", "uniqueness"
            )
        try:
            group = directory_groups.create(
                session, external_id, _label(data.get("displayName"))
            )
        except IntegrityError:
            raise ScimFailure(
                409, "A group with that externalId already exists", "uniqueness"
            ) from None
        members = _resolved(session, settings, caller, _group_refs(data.get("members")))
        affected = directory_groups.replace_principals(session, group, members)
        _apply_members(session, affected)
        _pushed_group(session, caller, group, "create")
        return _json(_group_body(request, session, group), 201, group.version)
    except ScimFailure as exc:
        return _fail(session, exc)


@router.get("/Groups/{group_id}")
def get_group(
    group_id: str,
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        _gate(session, caller)
        group = directory_groups.by_id(session, group_id)
        if group is None:
            raise ScimFailure(404, "Group not found")
        return _json(_group_body(request, session, group), etag=group.version)
    except ScimFailure as exc:
        return _fail(session, exc)


@router.patch("/Groups/{group_id}")
async def patch_group(
    group_id: str,
    request: Request,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    try:
        settings = _gate(session, caller)
        group = directory_groups.by_id(session, group_id, lock=True)
        if group is None:
            raise ScimFailure(404, "Group not found")
        _check_match(group.version or 1, if_match, "group")
        parsed = _group_patch(await _payload(request))
        for kind, payload in parsed:
            if kind == "display":
                _label(payload)
        working = directory_groups.principal_ids(session, group.group_id)
        members_touched = False
        for kind, payload in parsed:
            if kind == "display":
                directory_groups.rename(group, _label(payload))
            elif kind == "external" and payload and payload != group.external_id:
                trail_events.record(
                    session,
                    "directory.claim_ignored",
                    actor=caller.principal_id,
                    group_id=group.group_id,
                    directory_id=payload,
                )
            elif kind == "replace":
                working = _resolved(session, settings, caller, payload)
                members_touched = True
            elif kind == "add":
                working |= _resolved(session, settings, caller, payload)
                members_touched = True
            elif kind == "remove":
                working -= _resolved(session, settings, caller, payload)
                members_touched = True
        affected: set[str] = set()
        if members_touched:
            affected = directory_groups.replace_principals(session, group, working)
        # The membership change stands even when a role or a join is skipped.
        _apply_members(session, affected)
        _pushed_group(session, caller, group, "patch")
        return _json(_group_body(request, session, group), etag=group.version)
    except ScimFailure as exc:
        return _fail(session, exc)


@router.delete("/Groups/{group_id}", status_code=204)
def delete_group(
    group_id: str,
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
    if_match: Optional[str] = Header(None, alias="If-Match"),
):
    try:
        _gate(session, caller)
        group = directory_groups.by_id(session, group_id, lock=True)
        if group is None:
            raise ScimFailure(404, "Group not found")
        _check_match(group.version or 1, if_match, "group")
        external_id = group.external_id
        group_key = group.group_id
        held = directory_groups.delete(session, group)
        _apply_members(session, held)
        trail_events.record(
            session,
            "directory.pushed",
            actor=caller.principal_id,
            group_id=group_key,
            external_id=external_id,
            op="delete",
        )
        return Response(status_code=204)
    except ScimFailure as exc:
        return _fail(session, exc)
