"""Directory sync: the account that pushes, and the claim that joins it (#4423).

Turning sync on creates one role and one service account. The role holds only
``directory.sync`` and is not granted anywhere else. The account's token is
minted from the single sign-on card, not from the tokens screen.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Optional

from pydantic import ValidationError
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from core.audit import trail
from core.auth import (
    mcp_credential_service,
    principals,
    session_policy,
    sso,
    sso_settings,
    token_policy,
    token_unused,
    trail_events,
)
from core.auth.deactivation import clear_deactivation
from core.auth.identity import claim_sign_in_names, identifier_in_use
from core.storage.models import (
    McpCredential,
    Principal,
    Role,
    SsoLink,
    SystemConfig,
    User,
)
from core.time import utcnow

ROLE_ID = "role-directory-sync"
USERNAME = "directory-sync"
PERMISSION = "directory.sync"
TOKEN_LABEL = "Directory sync"
PURPOSE = "Pushes accounts from the identity provider."
EMAIL = f"{USERNAME}@service.invalid"

_CLAIM = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
# Profile claims a person can change, or that more than one person can hold. A
# sign-in carrying one would reach whoever's externalId it names.
_MUTABLE_CLAIMS = frozenset({
    "email", "emails", "preferred_username", "upn", "unique_name", "name",
    "nickname", "given_name", "family_name", "middle_name", "username",
    "login", "mail", "phone_number", "profile", "website", "samaccountname",
})  # fmt: skip


class SyncRefused(Exception):
    """A switch-on or a claim change that does not go ahead. ``detail`` is the response."""

    def __init__(self, detail: str, status: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def claim_name(value: str) -> str:
    """A sign-in claim name, such as ``sub`` or ``oid``."""
    name = (value or "").strip()
    if not _CLAIM.fullmatch(name):
        raise SyncRefused(
            "The claim name must be a single token, such as sub or oid", status=400
        )
    if not is_safe_claim(name):
        raise SyncRefused(
            f"{name} can be changed by the person or shared, so it cannot identify"
            " a pushed account. Use sub, or an immutable id such as oid.",
            status=400,
        )
    return name


def is_safe_claim(name: str) -> bool:
    return name.lower() not in _MUTABLE_CLAIMS


def claim_value(claims: dict, name: str) -> Optional[str]:
    """The configured claim when the token carries it as one non-empty string.

    A mutable claim saved before those were refused is never read."""
    if not is_safe_claim(name):
        return None
    value = claims.get(name)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 1024:
        return None
    return value


def is_sync_account(user: User) -> bool:
    return user.username == USERNAME or user.role_id == ROLE_ID


def note_directory_id(session: Session, link: SsoLink, issuer: str, value: str) -> None:
    """Refresh ``directory_id`` on a link the push does not manage.

    A managed link keeps the id a push stored. A different value is recorded
    and left unused. A value another link already holds is recorded and skipped.
    """
    if not value or link.directory_id == value:
        return
    if link.managed:
        trail_events.record(
            session,
            "directory.claim_ignored",
            actor=principals.SYSTEM_PRINCIPAL_ID,
            principal=link.principal_id,
            directory_id=value,
        )
        return
    taken = (
        session.query(SsoLink.link_id)
        .filter(
            SsoLink.issuer == issuer,
            SsoLink.directory_id == value,
            SsoLink.link_id != link.link_id,
        )
        .first()
    )
    if taken is not None:
        trail_events.record(
            session,
            "directory.id_taken",
            actor=principals.SYSTEM_PRINCIPAL_ID,
            principal=link.principal_id,
            directory_id=value,
        )
        return
    link.directory_id = value


def adopt_subject(session: Session, link: SsoLink, issuer: str, subject: str) -> None:
    """The first real sign-in replaces a placeholder subject (still the externalId).

    A subject another account already holds is recorded and left as it is.
    """
    # Managed or not: the placeholder subject is the externalId until the first
    # real sign-in. directory_id stays put once the push owns the link.
    if (
        not link.directory_id
        or link.subject != link.directory_id
        or subject == link.subject
    ):
        return
    if (
        session.query(SsoLink.link_id)
        .filter(SsoLink.issuer == issuer, SsoLink.subject == subject)
        .first()
        is not None
    ):
        trail_events.record(
            session,
            "directory.subject_taken",
            actor=principals.SYSTEM_PRINCIPAL_ID,
            principal=link.principal_id,
        )
        return
    link.subject = subject


def _write_settings(
    session: Session,
    found: bool,
    settings: sso_settings.SsoSettings,
    version: int,
    actor: User,
) -> None:
    value = {**settings.model_dump(mode="json"), "version": version}
    if not found:
        raise SyncRefused("Save single sign-on before turning directory sync on")
    row = session.get(SystemConfig, sso_settings.KEY)
    row.value = value
    row.updated_by = actor.user_id
    row.updated_at = utcnow()


def _ensure_role(session: Session) -> Role:
    role = session.get(Role, ROLE_ID)
    permissions = {PERMISSION: True}
    if role is None:
        taken = (
            session.query(Role.role_id)
            .filter(func.lower(Role.name) == "directory sync")
            .first()
        )
        if taken is not None:
            raise SyncRefused(
                "A role named Directory sync already exists. Rename it, then turn sync on."
            )
        role = Role(
            role_id=ROLE_ID,
            name="Directory sync",
            description="Pushes accounts from the identity provider. Not granted to anyone else.",
            permissions=permissions,
            is_system_role=False,
        )
        session.add(role)
        session.flush()
        return role
    role.permissions = permissions
    return role


def _ensure_account(session: Session, actor: User) -> User:
    _ensure_role(session)
    user = session.query(User).filter(User.username == USERNAME).one_or_none()
    if user is None:
        claim_sign_in_names(session, USERNAME, EMAIL)
        if identifier_in_use(session, USERNAME) or identifier_in_use(session, EMAIL):
            raise SyncRefused("The directory sync account name is already in use")
        user = User(
            user_id=f"user-{uuid.uuid4().hex[:12]}",
            username=USERNAME,
            email=EMAIL,
            password_hash=None,
            full_name="Directory sync",
            role_id=ROLE_ID,
            is_active=True,
            is_verified=True,
            service_account=True,
            purpose=PURPOSE,
            accountable_principal_id=actor.principal_id,
        )
        principals.add_for_user(session, user)
        session.add(user)
        session.flush()
        trail_events.record(
            session,
            "user.created",
            actor=actor.principal_id,
            principal=user.principal_id,
            role_id=ROLE_ID,
            service_account=True,
            accountable=actor.principal_id,
        )
        return user
    principal = session.get(Principal, user.principal_id)
    if principal is None or principal.deleted_at is not None:
        raise SyncRefused(
            "The directory sync account was deleted. Restore it before turning sync on."
        )
    if not user.service_account:
        raise SyncRefused("The directory sync account name belongs to a person")
    user.role_id = ROLE_ID
    user.is_active = True
    user.purpose = PURPOSE
    user.accountable_principal_id = actor.principal_id
    clear_deactivation(user)
    session.flush()
    return user


def _revoke_tokens(session: Session, account: User, actor: str) -> None:
    for credential in mcp_credential_service.list_for_user(
        account.user_id, session=session
    ):
        if mcp_credential_service.revoke(credential.credential_id, session=session):
            trail_events.record(
                session,
                "credential.revoked",
                actor=actor,
                principal=account.principal_id,
                credential_id=credential.credential_id,
            )


def _mint(
    session: Session, account: User, actor: str, sign_in: Optional[str] = None
) -> str:
    try:
        # The policy before the token rows, the order a save of it takes them in.
        token_policy.current(session)
        _revoke_tokens(session, account, actor)
        minted = mcp_credential_service.mint(
            account.user_id,
            TOKEN_LABEL,
            session=session,
            actor=actor,
            sign_in=sign_in,
        )
    except token_policy.PolicyUnreadable as exc:
        raise SyncRefused(str(exc)) from exc
    if minted is None:
        raise SyncRefused("Could not mint a token")
    return minted.token


def turn_on(
    session: Session, actor: User, claim: str, sign_in: Optional[str] = None
) -> str:
    """Turn sync on and mint its token. The token is returned once."""
    claim = claim_name(claim)
    # The policy before anything is recorded or locked: a save of it holds it, then
    # appends to the trail, and the account's creation below appends too.
    try:
        token_policy.current(session)
    except token_policy.PolicyUnreadable as exc:
        raise SyncRefused(str(exc)) from exc
    old, found = sso_settings.read_row(session, lock=True)
    if old.settings.sync_enabled:
        raise SyncRefused("Directory sync is already on")
    if not sso.is_on(old.settings):
        raise SyncRefused("Turn single sign-on on before directory sync")
    account = _ensure_account(session, actor)
    token = _mint(session, account, actor.principal_id, sign_in)
    version = old.version + 1
    new = old.settings.model_copy(
        update={"sync_enabled": True, "directory_id_claim": claim}
    )
    _write_settings(session, found, new, version, actor)
    trail_events.record(
        session,
        "directory.sync_on",
        actor=actor.principal_id,
        version=version,
        directory_id_claim=claim,
        accountable=actor.principal_id,
        # While sync is on its token is outside the unused-token rule.
        unused_token_exempt=True,
    )
    return token


def set_claim(session: Session, actor: User, claim: str) -> sso_settings.SsoSettings:
    claim = claim_name(claim)
    old, found = sso_settings.read_row(session, lock=True)
    if not old.settings.sync_enabled:
        raise SyncRefused("Directory sync is off")
    if claim == old.settings.directory_id_claim:
        return old.settings
    version = old.version + 1
    new = old.settings.model_copy(update={"directory_id_claim": claim})
    _write_settings(session, found, new, version, actor)
    trail_events.record(
        session,
        "directory.claim_changed",
        actor=actor.principal_id,
        version=version,
        directory_id_claim=claim,
    )
    return new


def remint(session: Session, actor: User, sign_in: Optional[str] = None) -> str:
    """Mint a replacement token and revoke the one it replaces. Sync stays on."""
    old, _found = sso_settings.read_row(session, lock=True)
    if not old.settings.sync_enabled:
        raise SyncRefused("Directory sync is off")
    account = session.query(User).filter(User.username == USERNAME).one_or_none()
    if account is None or not account.is_active:
        raise SyncRefused("The directory sync account is not active")
    return _mint(session, account, actor.principal_id, sign_in)


def turn_off(session: Session, reason: str) -> int:
    """Set ``sync_enabled`` false. The token is left as it is. Returns the version."""
    reason = reason.strip()
    if not reason:
        raise ValueError("a reason is required")
    old, found = sso_settings.read_row(session, lock=True)
    if not found or not old.settings.sync_enabled:
        raise SyncRefused("Directory sync is already off")
    version = old.version + 1
    new = old.settings.model_copy(update={"sync_enabled": False})
    row = session.get(SystemConfig, sso_settings.KEY)
    row.value = {**new.model_dump(mode="json"), "version": version}
    row.updated_at = utcnow()
    session.flush()
    if trail.trail_ready(session):
        trail.append(
            session,
            {
                trail.ACTION: "directory.sync_off",
                trail.ACTOR: trail.SYSTEM,
                "version": version,
                "reason": reason,
            },
        )
    return version


def _trail_ready(session: Session) -> bool:
    try:
        return trail_events.ready(session)
    except Exception:  # noqa: BLE001 - status still answers
        return False


def latest_push(session: Session) -> Optional[datetime]:
    if not _trail_ready(session):
        return None
    row = session.execute(text("""
            SELECT occurred_at FROM public.audit_trail
            WHERE payload->>'action' = 'directory.pushed'
            ORDER BY seq DESC LIMIT 1
        """)).first()
    return row[0] if row else None


def failed_authentications(session: Session) -> int:
    if not _trail_ready(session):
        return 0
    row = session.execute(text("""
            SELECT count(*) FROM public.audit_trail
            WHERE payload->>'action' = 'directory.auth_failed'
        """)).first()
    return int(row[0] if row else 0)


def last_manager_ids(session: Session) -> list[str]:
    """User ids a push refused as the last role manager, newest first, once each.

    A refusal stays open until the account is inactive (checked by the caller), a
    later push for it sets ``active: true``, or it was enabled again after being
    deactivated. Another person gaining roles.manage does not settle it: nothing
    applies the refused deactivation then.
    """
    if not _trail_ready(session):
        return []
    rows = session.execute(text("""
            SELECT r.user_id FROM (
                SELECT DISTINCT ON (payload->>'user_id')
                       payload->>'user_id' AS user_id,
                       payload->>'principal' AS principal, seq
                FROM public.audit_trail
                WHERE payload->>'action' = 'directory.last_manager'
                ORDER BY payload->>'user_id', seq DESC
            ) r
            WHERE r.user_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.audit_trail p
                WHERE p.payload->>'principal' = r.principal
                  AND (p.payload->>'action' = 'user.enabled'
                       OR (p.payload->>'action' = 'directory.pushed'
                           AND p.payload->>'active' = 'true'))
                  AND p.seq > r.seq
            )
            ORDER BY r.seq DESC LIMIT 200
        """)).all()
    return [user_id for (user_id,) in rows]


def sync_token(session: Session) -> Optional[McpCredential]:
    """The directory sync account's usable token, if it has one."""
    account = session.query(User).filter(User.username == USERNAME).one_or_none()
    if account is None:
        return None
    for credential in mcp_credential_service.list_for_user(
        account.user_id, session=session
    ):
        if credential.label == TOKEN_LABEL and credential.is_usable():
            return credential
    return None


def token_expiry(session: Session) -> Optional[datetime]:
    credential = sync_token(session)
    return credential.expires_at if credential else None


def first_reminder(session: Session) -> Optional[datetime]:
    """When the sync token's first unused-token reminder would come, as if sync were
    off. None with no usable token, no rule, or a rule that can't be read."""
    credential = sync_token(session)
    state = token_unused.read_state(session)
    if credential is None or state is None:
        return None
    return token_unused.first_reminder(credential, state)


def unused_rule_text(session: Session) -> str:
    """One sentence for where the sync token stands under the unused-token rule while
    sync is off, for the host command."""
    if sync_token(session) is None:
        return "There is no usable directory sync token."
    when = first_reminder(session)
    if when is not None:
        return (
            "The token is now under the unused-token rule: if it is not used, its first "
            f"reminder comes on {when:%Y-%m-%d} UTC."
        )
    try:
        off = session_policy.read_row(session)[0].policy.token_unused_days is None
    except (ValidationError, TypeError, ValueError):
        off = False
    if off:
        return (
            "The unused-token period is off, so the token is not subject to the rule."
        )
    return (
        "The unused-token rule could not be read, so no first reminder date is given."
    )
