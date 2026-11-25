"""Single sign-on settings: the provider, its client, and group → role mapping (#4283).

One row, versioned like the session policy: GET returns it with its version as
the ETag; PUT needs that version in ``If-Match`` and refuses a stale one with
412. A save that changes the discovery URL or client ID while a client secret
is stored, and still names both, must send the new provider's secret. That
save is refused and nothing is fetched. A save that does change the provider
or the secret first fetches the discovery document and refuses, with a fixed
reason, unless it answers. The client secret is written to the secrets store
and never returned.

"Require single sign-on" (#4286) is a field of the same row. It turns on only
from the caller's own SSO sign-in that started after the discovery URL or client
ID last changed; while on, those two can't change and SSO can't be turned off.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.dialects.postgresql import insert

from core.auth import (
    directory_groups,
    directory_sync,
    sign_ins,
    sso,
    sso_settings,
    trail_events,
)
from core.auth.auth_cookies import context_path_prefix
from core.auth.password_reset import default_base_url
from core.auth.permissions import (
    deny_unless,
    require_permission,
    require_person_permission,
)
from core.config import get_settings
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.secrets import get_secret, secret_write_failure_detail, set_secret
from core.storage.models import DirectoryPending, Role, SignIn, SystemConfig, User
from core.storage.schemas.base import OptDateTime
from core.time import utcnow
from services.api import group_mapping
from services.api.routers.auth import _session_sid
from services.api.routers.users import _can_assign_role

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/auth/sso",
    tags=["sso"],
    auth=Auth.REQUIRED,
)

_MANAGE = require_permission("roles.manage")
# Every write here can widen access (group and default roles, turning off Require
# single sign-on, choosing the provider), so it is a person's own act: a service
# account or an API token may read but not save.
_PERSON_MANAGE = require_person_permission("roles.manage", refuse_machine=True)

NOT_REACHED = (
    "Single sign-on can be turned on only after a save has reached the provider"
)
NEEDS_CLIENT = (
    "Single sign-on needs a client ID and a client secret before it is turned on"
)
# A stored secret belongs to the discovery URL and client ID it was saved with.
NEEDS_NEW_SECRET = (
    "Enter the client secret for this provider. The saved one belongs to "
    "the previous discovery URL or client ID"
)
# Why Require single sign-on can't be turned on from this sign-in.
REQUIRE_SSO_OFF = "Single sign-on is not on and saved"
REQUIRE_NOT_SSO = "Your sign-in didn't come through SSO: sign out and sign in with SSO"
REQUIRE_STALE = (
    "Your SSO sign-in started before the discovery URL or client ID last changed: "
    "sign in with SSO again"
)
REQUIRE_FIRST = "Turn off Require single sign-on first"
SYNC_HOLDS_PROVIDER = (
    "Directory sync is on, so single sign-on stays on and the discovery URL "
    "and client ID stay as they are. Turn directory sync off on the host first: "
    "python -m core.auth.directory_sync_off"
)


def require_blockers(
    s: sso_settings.SsoSettings, request: Request, session
) -> list[str]:
    """What stops the caller turning Require single sign-on on under ``s``; empty: nothing."""
    blockers = [] if sso.is_on(s) else [REQUIRE_SSO_OFF]
    sid = _session_sid(request)
    row = session.get(SignIn, sid) if sid else None
    if row is None or row.auth_method != sign_ins.SSO:
        blockers.append(REQUIRE_NOT_SSO)
    elif s.provider_changed_at is None or row.started_at <= s.provider_changed_at:
        blockers.append(REQUIRE_STALE)
    return blockers


class SsoSettingsIn(BaseModel):
    """What a save sends. ``issuer`` comes from the check."""

    enabled: bool = False
    discovery_url: str = Field("", max_length=2048)
    client_id: str = Field("", max_length=512)
    # Empty or absent keeps the stored secret, unless the discovery URL or
    # client ID changes while one is stored.
    client_secret: Optional[SecretStr] = None
    group_roles: list[sso_settings.GroupRole] = Field(
        default_factory=list, max_length=200
    )
    default_role_id: str = sso_settings.DEFAULT_ROLE
    groups_claim: str = Field("groups", min_length=1, max_length=128)
    # None or absent keeps the stored value.
    require_sso: Optional[bool] = None


class SsoSettingsOut(sso_settings.SsoSettings):
    client_secret_set: bool
    redirect_url: str
    # False: the redirect address is this machine's own, and the card warns.
    frontend_url_set: bool
    version: int
    updated_by: Optional[str] = None
    updated_by_name: Optional[str] = None
    updated_at: OptDateTime = None
    # Why the caller can't turn Require single sign-on on now; empty: they can.
    require_blockers: list[str] = []


class SsoStaleOut(BaseModel):
    detail: str
    current: SsoSettingsOut


def _out(stored: sso_settings.Stored, session, request: Request) -> SsoSettingsOut:
    editor = stored.updated_by and session.get(User, stored.updated_by)
    config = get_settings()
    return SsoSettingsOut(
        **stored.settings.model_dump(),
        client_secret_set=bool(get_secret(sso_settings.SECRET_KEY)),
        redirect_url=default_base_url(config).rstrip("/") + sso_settings.CALLBACK_PATH,
        frontend_url_set=bool(config.vigil_frontend_url),
        version=stored.version,
        updated_by=stored.updated_by,
        updated_by_name=(editor.full_name or editor.username) if editor else None,
        updated_at=stored.updated_at,
        require_blockers=require_blockers(stored.settings, request, session),
    )


def _etag(version: int) -> str:
    return f'"{version}"'


def _version_of(if_match: Optional[str]) -> int:
    if if_match is None:
        raise HTTPException(status_code=428, detail="If-Match is required")
    try:
        return int(if_match.removeprefix("W/").strip().strip('"'))
    except ValueError:
        raise HTTPException(
            status_code=400, detail="If-Match must be a version from GET"
        ) from None


@router.get("/settings", response_model=SsoSettingsOut)
def get_sso_settings(
    request: Request,
    response: Response,
    session: UnitOfWorkSession,
    _: User = Depends(_MANAGE),
):
    """The settings, whether a secret is stored, the redirect address, and the version (the ETag)."""
    stored, _found = sso_settings.read_row(session)
    response.headers["ETag"] = _etag(stored.version)
    return _out(stored, session, request)


@router.put(
    "/settings",
    response_model=SsoSettingsOut,
    responses={
        412: {
            "model": SsoStaleOut,
            "description": "If-Match is not the current version",
        },
        428: {"description": "If-Match is missing"},
    },
)
def put_sso_settings(
    body: SsoSettingsIn,
    request: Request,
    response: Response,
    session: UnitOfWorkSession,
    user: User = Depends(_PERSON_MANAGE),
    if_match: Optional[str] = Header(None),
):
    """Replace the settings, checking the provider when its address or client changes."""
    expected = _version_of(if_match)

    def stale():
        current = _out(sso_settings.read_row(session)[0], session, request)
        return JSONResponse(
            status_code=412,
            content=SsoStaleOut(
                detail="The single sign-on settings changed since you read them",
                current=current,
            ).model_dump(mode="json"),
            headers={"ETag": _etag(current.version)},
        )

    # The row stays locked until this request ends.
    old, found = sso_settings.read_row(session, lock=True)
    if old.version != expected:
        return stale()

    secret = body.client_secret.get_secret_value().strip() if body.client_secret else ""
    new = sso_settings.SsoSettings(
        enabled=body.enabled,
        discovery_url=body.discovery_url.strip(),
        client_id=body.client_id.strip(),
        issuer=old.settings.issuer,
        group_roles=(
            old.settings.group_roles
            if old.settings.sync_enabled
            else [
                sso_settings.GroupRole(group=g.group.strip(), role_id=g.role_id)
                for g in body.group_roles
            ]
        ),
        team_lines=old.settings.team_lines,
        mapping_author=old.settings.mapping_author,
        default_role_id=body.default_role_id,
        groups_claim=body.groups_claim.strip(),
        require_sso=(
            old.settings.require_sso if body.require_sso is None else body.require_sso
        ),
        provider_changed_at=old.settings.provider_changed_at,
        sync_enabled=old.settings.sync_enabled,
        directory_id_claim=old.settings.directory_id_claim,
    )
    changes = {
        "old": old.settings.model_dump(mode="json"),
        "new": new.model_dump(mode="json"),
        "client_secret_changed": bool(secret),
    }

    def refuse(status: int, reason: str):
        trail_events.refused(
            "sso_settings.refused", actor=user.principal_id, reason=reason, **changes
        )
        raise HTTPException(status_code=status, detail=reason)

    # Every role a group could grant, and the default, must be one the caller could assign by hand.
    # While sync is on the mapping save is the only writer of those lines, and a
    # raise is judged then, per account. This save leaves the lines as stored.
    uses = (
        []
        if old.settings.sync_enabled
        else [(g.role_id, "map a group to") for g in new.group_roles]
    )
    for role_id, use in [*uses, (new.default_role_id, "make the default role")]:
        role = session.get(Role, role_id)
        if role is None:
            refuse(403, f"There is no role {role_id}")
        if not _can_assign_role(user, role, session):
            refuse(403, f"You can't {use} {role.name}: it grants more than you hold")

    client_changed = (
        new.discovery_url != old.settings.discovery_url
        or new.client_id != old.settings.client_id
    )
    # While directory sync is on, the provider it pushes against stays put.
    if old.settings.sync_enabled and (client_changed or not new.enabled):
        refuse(409, SYNC_HOLDS_PROVIDER)
    # While required, the provider people sign in through stays put.
    if old.settings.require_sso and (client_changed or not new.enabled):
        refuse(409, REQUIRE_FIRST)
    # The stored secret was issued for the previous registration. On or off,
    # a different discovery URL or client ID needs this provider's secret
    # when both are still set. A cleared field names no provider, so it is not asked for.
    if (
        client_changed
        and new.discovery_url
        and new.client_id
        and not secret
        and get_secret(sso_settings.SECRET_KEY)
    ):
        refuse(409, NEEDS_NEW_SECRET)
    # A row saved before the timestamp was kept gets one on its next save.
    if client_changed or (new.discovery_url and new.provider_changed_at is None):
        new.provider_changed_at = utcnow()
    provider_changed = client_changed or bool(secret)
    if provider_changed or (new.discovery_url and not new.issuer):
        if new.discovery_url:
            try:
                new.issuer = sso_settings.discover(new.discovery_url)
            except sso_settings.DiscoveryRefused as refusal:
                refuse(400, refusal.reason)
        else:
            new.issuer = None
        changes["new"] = new.model_dump(mode="json")

    if new.enabled:
        if not new.issuer:
            refuse(409, NOT_REACHED)
        if not new.client_id or not (secret or get_secret(sso_settings.SECRET_KEY)):
            refuse(409, NEEDS_CLIENT)

    turning_on = new.require_sso and not old.settings.require_sso
    if turning_on and (blockers := require_blockers(new, request, session)):
        refuse(409, "; ".join(blockers))

    value = {**new.model_dump(mode="json"), "version": old.version + 1}
    if found:
        row = session.get(SystemConfig, sso_settings.KEY)
        row.value, row.updated_by, row.updated_at = value, user.user_id, utcnow()
    else:
        # A concurrent first save holds the same key: this one loses, with 412.
        inserted = session.execute(
            insert(SystemConfig)
            .values(
                key=sso_settings.KEY,
                value=value,
                description="Single sign-on: provider and group roles",
                config_type="auth",
                updated_by=user.user_id,
            )
            .on_conflict_do_nothing()
        )
        if inserted.rowcount == 0:
            return stale()
    trail_events.record(
        session,
        "sso_settings.changed",
        actor=user.principal_id,
        version=old.version + 1,
        **changes,
    )
    if new.require_sso != old.settings.require_sso:
        trail_events.record(
            session,
            "sso.required_on" if new.require_sso else "sso.required_off",
            actor=user.principal_id,
            version=old.version + 1,
        )
    session.flush()
    # Last, so a save that loses above never replaces the stored secret; a failure here rolls the row back.
    if secret and not set_secret(sso_settings.SECRET_KEY, secret):
        refuse(500, secret_write_failure_detail("The client secret was not saved"))
    stored = sso_settings.read_row(session)[0]
    response.headers["ETag"] = _etag(stored.version)
    return _out(stored, session, request)


_SYNC_PERMS = ("roles.manage", "users.write", "teams.manage")


class SyncIn(BaseModel):
    directory_id_claim: str = Field("sub", min_length=1, max_length=128)
    # When sync is already on, mint a replacement token and revoke the previous one.
    mint: bool = False


class BrokenLine(BaseModel):
    external_id: str
    # "role" or "team": which list still names a group the provider has deleted.
    on: str


class SyncOut(BaseModel):
    sync_enabled: bool
    directory_id_claim: str
    last_push: Optional[str] = None
    token_expires_at: Optional[str] = None
    # While sync is off: when its token's first unused-token reminder would come.
    first_reminder_at: Optional[str] = None
    failed_authentications: int
    last_manager_refusals: list[str]
    skipped_joins: int = 0
    # Pushes refused for a username or email in use, waiting for a link.
    pending_links: int = 0
    broken_lines: list[BrokenLine] = []
    # Set until a mapping is saved, and again when that person is gone or inactive.
    mapping_note: Optional[str] = None
    role_refusals: list[str] = []
    scim_path: str
    # Present only in the response that mints it.
    token: Optional[str] = None


def _stamp(value) -> Optional[str]:
    if value is None:
        return None
    text = value.isoformat()
    if getattr(value, "tzinfo", None) is None:
        return text + "Z"
    return text


def _sync_caller(user: User, session) -> None:
    for permission in _SYNC_PERMS:
        deny_unless(user.user_id, permission, session)


def _sync_out(session, token: Optional[str] = None) -> SyncOut:
    settings = sso_settings.read_row(session)[0].settings
    # Listed until applied: the floor lifting does not deactivate the account.
    refusals = []
    for user_id in directory_sync.last_manager_ids(session):
        held = session.get(User, user_id)
        if held is not None and held.is_active:
            refusals.append(held.username)
    return SyncOut(
        sync_enabled=settings.sync_enabled,
        directory_id_claim=settings.directory_id_claim,
        last_push=_stamp(directory_sync.latest_push(session)),
        token_expires_at=_stamp(directory_sync.token_expiry(session)),
        first_reminder_at=(
            None
            if settings.sync_enabled
            else _stamp(directory_sync.first_reminder(session))
        ),
        failed_authentications=directory_sync.failed_authentications(session),
        last_manager_refusals=refusals,
        skipped_joins=group_mapping.skipped_joins(session),
        pending_links=session.query(DirectoryPending).count(),
        broken_lines=[
            BrokenLine(**line) for line in group_mapping.broken_lines(session, settings)
        ],
        mapping_note=group_mapping.mapping_note(session, settings),
        role_refusals=group_mapping.role_refusals(session, settings),
        scim_path=f"{context_path_prefix()}/scim/v2",
        token=token,
    )


@router.get("/sync", response_model=SyncOut)
def get_sync(session: UnitOfWorkSession, _: User = Depends(_MANAGE)):
    """Whether directory sync is on, and what the last push did. No token."""
    return _sync_out(session)


@router.post("/sync", response_model=SyncOut)
def post_sync(
    request: Request,
    body: SyncIn,
    session: UnitOfWorkSession,
    user: User = Depends(_PERSON_MANAGE),
):
    """Turn directory sync on, or change its claim, and optionally mint a new token.

    Turning it on mints the token once. Minting again revokes the previous one.
    Needs roles.manage, users.write and teams.manage, and one step-up.
    """
    _sync_caller(user, session)
    try:
        current = sso_settings.read_row(session)[0].settings
        claims = getattr(request.state, "access_claims", None)
        sign_in = claims.get("sid") if isinstance(claims, dict) else None
        token = None
        if not current.sync_enabled:
            token = directory_sync.turn_on(
                session, user, body.directory_id_claim, sign_in
            )
        else:
            directory_sync.set_claim(session, user, body.directory_id_claim)
            if body.mint:
                token = directory_sync.remint(session, user, sign_in)
    except directory_sync.SyncRefused as refusal:
        raise HTTPException(status_code=refusal.status, detail=refusal.detail) from None
    return _sync_out(session, token)


class PushedGroup(BaseModel):
    id: str
    external_id: str
    display_name: str


class MappingOut(BaseModel):
    group_roles: list[sso_settings.GroupRole]
    team_lines: list[sso_settings.TeamLine]
    groups: list[PushedGroup]
    version: int
    mapping_author: Optional[str] = None


class MappingIn(BaseModel):
    group_roles: list[sso_settings.GroupRole] = Field(
        default_factory=list, max_length=200
    )
    team_lines: list[sso_settings.TeamLine] = Field(
        default_factory=list, max_length=200
    )


class MappingEffect(BaseModel):
    roles_rise: int = 0
    roles_fall: int = 0
    roles_default: int = 0
    memberships_added: int = 0
    memberships_removed: int = 0
    applied: bool
    version: int


def _mapping_out(stored: sso_settings.Stored, session) -> MappingOut:
    return MappingOut(
        group_roles=stored.settings.group_roles,
        team_lines=stored.settings.team_lines,
        groups=[
            PushedGroup(
                id=group.group_id,
                external_id=group.external_id,
                display_name=group.display_name,
            )
            for group in directory_groups.list_groups(session)
        ],
        version=stored.version,
        mapping_author=stored.settings.mapping_author,
    )


def _clean_lines(
    body: MappingIn,
) -> tuple[list[sso_settings.GroupRole], list[sso_settings.TeamLine]]:
    roles = [
        sso_settings.GroupRole(group=line.group.strip(), role_id=line.role_id)
        for line in body.group_roles
    ]
    team_lines = []
    for line in body.team_lines:
        group = line.group.strip()
        team_id = line.team_id.strip()
        if not group or not team_id:
            raise HTTPException(
                status_code=400, detail="A mapping line needs a group and a team"
            )
        team_lines.append(sso_settings.TeamLine(group=group, team_id=team_id))
    return roles, team_lines


@router.get("/mapping", response_model=MappingOut)
def get_mapping(session: UnitOfWorkSession, _: User = Depends(_MANAGE)):
    """Pushed groups and the role and team lines. The version is the settings version."""
    return _mapping_out(sso_settings.read_row(session)[0], session)


@router.post("/mapping", response_model=MappingEffect)
def post_mapping(
    body: MappingIn,
    session: UnitOfWorkSession,
    user: User = Depends(_PERSON_MANAGE),
    if_match: Optional[str] = Header(None),
    dry_run: bool = False,
):
    """Save the group mapping, or count what saving it would change.

    ``dry_run`` writes nothing. The real save is the one that sets
    ``mapping_author`` and applies roles and team membership. Needs
    roles.manage, users.write and teams.manage, and one step-up.
    """
    _sync_caller(user, session)
    expected = _version_of(if_match)
    old, found = sso_settings.read_row(session, lock=not dry_run)
    if not found or not old.settings.sync_enabled:
        raise HTTPException(status_code=409, detail="Directory sync is off")
    if old.version != expected:
        raise HTTPException(
            status_code=412,
            detail="The single sign-on settings changed since you read them",
        )
    roles, team_lines = _clean_lines(body)
    for role_id in {line.role_id for line in roles}:
        if session.get(Role, role_id) is None:
            raise HTTPException(status_code=403, detail=f"There is no role {role_id}")
    if group_mapping.names_default_team(session, team_lines):
        raise HTTPException(status_code=409, detail=group_mapping.DEFAULT_TEAM)
    lines_changed = [(line.group, line.role_id) for line in roles] != [
        (line.group, line.role_id) for line in old.settings.group_roles
    ] or [(line.group, line.team_id) for line in team_lines] != [
        (line.group, line.team_id) for line in old.settings.team_lines
    ]
    stored_author = group_mapping.author_of(session, old.settings)
    if lines_changed or stored_author is None:
        author_id = user.user_id
        acting = user
    else:
        author_id = old.settings.mapping_author
        acting = stored_author
    new = old.settings.model_copy(
        update={
            "group_roles": roles,
            "team_lines": team_lines,
            "mapping_author": author_id,
        }
    )
    writing = (not dry_run) and (
        lines_changed or author_id != old.settings.mapping_author
    )
    version = old.version + 1 if writing else old.version
    if writing:
        row = session.get(SystemConfig, sso_settings.KEY)
        row.value = {**new.model_dump(mode="json"), "version": version}
        row.updated_by = user.user_id
        row.updated_at = utcnow()
        session.flush()
        trail_events.record(
            session,
            "directory.mapping_saved",
            actor=user.principal_id,
            version=version,
        )
    effect = group_mapping.reconcile(
        session,
        new,
        acting,
        principals=None,
        version=version,
        write=not dry_run,
    )
    return MappingEffect(**effect.as_dict(), applied=not dry_run, version=version)
