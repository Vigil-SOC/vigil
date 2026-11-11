"""Single sign-on settings: the provider, its client, and group → role mapping (#4283).

One row, versioned like the session policy: GET returns it with its version as
the ETag; PUT needs that version in ``If-Match`` and refuses a stale one with
412. A save that changes the discovery URL, client ID or secret first fetches
the provider's discovery document and refuses, with a fixed reason, unless it
answers. The client secret is written to the secrets store and never returned.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy.dialects.postgresql import insert

from core.auth import sso_settings, trail_events
from core.auth.password_reset import default_base_url
from core.auth.permissions import require_permission
from core.config import get_settings
from core.routing import Auth, RouterMeta, UnitOfWorkSession
from core.secrets import get_secret, secret_write_failure_detail, set_secret
from core.storage.models import Role, SystemConfig, User
from core.storage.schemas.base import OptDateTime
from core.time import utcnow
from services.api.routers.users import _can_assign_role

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/auth/sso",
    tags=["sso"],
    auth=Auth.REQUIRED,
)

_MANAGE = require_permission("roles.manage")

NOT_REACHED = (
    "Single sign-on can be turned on only after a save has reached the provider"
)
NEEDS_CLIENT = (
    "Single sign-on needs a client ID and a client secret before it is turned on"
)


class SsoSettingsIn(BaseModel):
    """What a save sends. ``issuer`` comes from the check and ``require_sso`` is not changed here."""

    enabled: bool = False
    discovery_url: str = Field("", max_length=2048)
    client_id: str = Field("", max_length=512)
    # Empty or absent keeps the stored secret.
    client_secret: Optional[SecretStr] = None
    group_roles: list[sso_settings.GroupRole] = Field(
        default_factory=list, max_length=200
    )
    default_role_id: str = sso_settings.DEFAULT_ROLE
    groups_claim: str = Field("groups", min_length=1, max_length=128)


class SsoSettingsOut(sso_settings.SsoSettings):
    client_secret_set: bool
    redirect_url: str
    # False: the redirect address is this machine's own, and the card warns.
    frontend_url_set: bool
    version: int
    updated_by: Optional[str] = None
    updated_by_name: Optional[str] = None
    updated_at: OptDateTime = None


class SsoStaleOut(BaseModel):
    detail: str
    current: SsoSettingsOut


def _out(stored: sso_settings.Stored, session) -> SsoSettingsOut:
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
    response: Response, session: UnitOfWorkSession, _: User = Depends(_MANAGE)
):
    """The settings, whether a secret is stored, the redirect address, and the version (the ETag)."""
    stored, _found = sso_settings.read_row(session)
    response.headers["ETag"] = _etag(stored.version)
    return _out(stored, session)


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
    response: Response,
    session: UnitOfWorkSession,
    user: User = Depends(_MANAGE),
    if_match: Optional[str] = Header(None),
):
    """Replace the settings, checking the provider when its address or client changes."""
    expected = _version_of(if_match)

    def stale():
        current = _out(sso_settings.read_row(session)[0], session)
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
        group_roles=[
            sso_settings.GroupRole(group=g.group.strip(), role_id=g.role_id)
            for g in body.group_roles
        ],
        default_role_id=body.default_role_id,
        groups_claim=body.groups_claim.strip(),
        require_sso=old.settings.require_sso,
    )
    changes = {
        "old": old.settings.model_dump(),
        "new": new.model_dump(),
        "client_secret_changed": bool(secret),
    }

    def refuse(status: int, reason: str):
        trail_events.refused(
            "sso_settings.refused", actor=user.principal_id, reason=reason, **changes
        )
        raise HTTPException(status_code=status, detail=reason)

    # Every role a group could grant, and the default, must be one the caller could assign by hand.
    uses = [(g.role_id, "map a group to") for g in new.group_roles]
    for role_id, use in [*uses, (new.default_role_id, "make the default role")]:
        role = session.get(Role, role_id)
        if role is None:
            refuse(403, f"There is no role {role_id}")
        if not _can_assign_role(user, role, session):
            refuse(403, f"You can't {use} {role.name}: it grants more than you hold")

    provider_changed = (
        new.discovery_url != old.settings.discovery_url
        or new.client_id != old.settings.client_id
        or bool(secret)
    )
    if provider_changed or (new.discovery_url and not new.issuer):
        if new.discovery_url:
            try:
                new.issuer = sso_settings.discover(new.discovery_url)
            except sso_settings.DiscoveryRefused as refusal:
                refuse(400, refusal.reason)
        else:
            new.issuer = None
        changes["new"] = new.model_dump()

    if new.enabled:
        if not new.issuer:
            refuse(409, NOT_REACHED)
        if not new.client_id or not (secret or get_secret(sso_settings.SECRET_KEY)):
            refuse(409, NEEDS_CLIENT)

    value = {**new.model_dump(), "version": old.version + 1}
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
    session.flush()
    # Last, so a save that loses above never replaces the stored secret; a failure here rolls the row back.
    if secret and not set_secret(sso_settings.SECRET_KEY, secret):
        refuse(500, secret_write_failure_detail("The client secret was not saved"))
    stored = sso_settings.read_row(session)[0]
    response.headers["ETag"] = _etag(stored.version)
    return _out(stored, session)
