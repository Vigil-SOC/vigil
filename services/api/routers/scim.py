"""Directory sync's SCIM User endpoint (#4423).

Mounted at ``/scim/v2``. Every handler answers 404 while sync is off and 403
for anyone but the directory-sync account. The body and the Authorization
header are never logged.

No groups, no bulk, no library: the handlers call the account functions.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from core.audit.retention import is_erased
from core.auth import directory_sync, principals, sso, sso_settings, trail_events
from core.auth.auth_cookies import context_path_prefix
from core.auth.deactivation import clear_deactivation, deactivate_account
from core.auth.identity import claim_sign_in_names, identifier_in_use
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.storage.models import Principal, Role, SsoLink, User
from core.teams import join_default
from core.time import utcnow
from services.api.middleware.auth import get_current_active_user
from services.api.routers.users import _leaves_no_manager as leaves_no_manager

router = APIRouter()

ROUTER_META = RouterMeta(prefix="/scim/v2", tags=["scim"], auth=Auth.REQUIRED)

_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
_PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
_SP = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
_MEDIA = "application/scim+json"
_EQ = re.compile(
    r"^\s*(userName|externalId|active)\s+eq\s+"
    r'(?:"((?:[^"\\]|\\.)*)"|(true|false))\s*$',
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


def _names(user_name: str, data: dict) -> tuple[str, str, Optional[str]]:
    """Username, email and display name from a SCIM user.

    Email is the work email, or ``userName`` when that is an email. Username
    is ``userName`` when it is not an email, otherwise the local part. No suffix.
    """
    user_name = user_name.strip()
    email = _work_email(data) or (user_name if "@" in user_name else "")
    username = user_name.split("@", 1)[0].strip() if "@" in user_name else user_name
    if not username or len(username) > 100:
        raise ScimFailure(400, "userName is not a usable username", "invalidValue")
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


def _skip(session: Session, caller: User, user: User, attribute: str) -> None:
    trail_events.record(
        session,
        "directory.attribute_skipped",
        actor=caller.principal_id,
        principal=user.principal_id,
        attribute=attribute,
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
    if username and username.casefold() != user.username.casefold():
        claim_sign_in_names(session, username)
        if identifier_in_use(session, username, except_user_id=user.user_id):
            _skip(session, caller, user, "username")
        else:
            user.username = username
            changed = True
    if email and email.casefold() != user.email.casefold():
        claim_sign_in_names(session, email)
        if identifier_in_use(session, email, except_user_id=user.user_id):
            _skip(session, caller, user, "email")
        else:
            user.email = email
            changed = True
    if full_name and full_name != user.full_name:
        user.full_name = full_name
        changed = True
    return changed


def _collide(
    session: Session, caller: User, external_id: str, username: str, email: str
) -> bool:
    claim_sign_in_names(session, username, email)
    hit = []
    if identifier_in_use(session, username):
        hit.append("username")
    if identifier_in_use(session, email):
        hit.append("email")
    if not hit:
        return False
    trail_events.record(
        session,
        "directory.collision",
        actor=caller.principal_id,
        external_id=external_id,
        attributes=hit,
    )
    return True


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
    session: Session, caller: User, external_id: Optional[str], user: User, op: str
) -> None:
    trail_events.record(
        session,
        "directory.pushed",
        actor=caller.principal_id,
        principal=user.principal_id,
        external_id=external_id,
        op=op,
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
    version = _version_of(header)
    if version is not None and version != user.scim_version:
        raise ScimFailure(412, "The user has changed since it was read")


def _returned_user_name(user: User) -> str:
    local, _, domain = user.email.partition("@")
    if domain and local.casefold() == user.username.casefold():
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
        folded = value.casefold()
        return query.filter(
            or_(
                func.lower(User.username) == folded,
                func.lower(User.email) == folded,
            )
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
    if user is None:
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
    if _collide(session, caller, external_id, username, email):
        raise ScimFailure(409, "That username or email is already in use", "uniqueness")
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
        if name not in ("add", "replace"):
            raise ScimFailure(400, "Only add and replace are supported", "invalidValue")
        path = op.get("path")
        value = op.get("value")
        if path is None and isinstance(value, dict):
            fields.update(_resource_fields(value))
            continue
        if not isinstance(path, str):
            raise ScimFailure(400, "An operation needs a path", "invalidValue")
        short = path.split(":")[-1]
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
    return fields


def _fields_to_names(
    user: User, fields: dict
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Username, email and display name a patch wants, or None to leave each."""
    username = email = None
    if isinstance(fields.get("userName"), str) and fields["userName"].strip():
        user_name = fields["userName"].strip()
        email = user_name if "@" in user_name else None
        username = user_name.split("@", 1)[0].strip() if "@" in user_name else user_name
        if not username or len(username) > 100:
            raise ScimFailure(400, "userName is not a usable username", "invalidValue")
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
        one = _user_type(request)
        return _json(
            {
                "schemas": [_LIST],
                "totalResults": 1,
                "Resources": [one],
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


@router.get("/Schemas")
def schemas(
    session: UnitOfWorkSession,
    caller: User = Depends(get_current_active_user),
):
    try:
        _gate(session, caller)
        return _json(
            {"schemas": [_LIST], "totalResults": 1, "Resources": [_user_schema()]}
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
        if schema_id != _USER:
            raise ScimFailure(404, "No such schema")
        return _json(_user_schema())
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
        _pushed(session, caller, external_id, user, "create")
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
        if changed:
            _touch(user)
        _pushed(session, caller, link.directory_id, user, "patch")
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
