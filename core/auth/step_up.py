"""Confirm it's you (#4179, epic #4164 decision 11).

A privileged change asks the person again unless their sign-in confirmed inside
the confirmation window. The window is the privileged idle limit
(``session_policy.idle_privileged_minutes``), up to 12 hours; when that limit
is 0 (no idle limit) the window is 10 minutes. It is ``sign_ins.confirmed_at``:
completing MFA at sign-in, a one-factor confirmation or a strong check sets it.
With Require MFA off, a password confirmation sets it too, and so does a
password sign-in by a person who holds no second factor. Which permissions ask is
``permissions.STEP_UP``, declared beside the registry. Agent runs, service
accounts and every other non-person caller are never asked.

Three failed confirmations in a row end the sign-in and never lock the account.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import NoReturn, Optional
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from core.auth import security_keys, session_policy, sign_ins, sso, trail_events
from core.auth.auth_service import AuthService
from core.auth.current_user import get_current_user
from core.storage.models import SignIn, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

# The 403's detail.code: confirm, or set up MFA first (a person without it).
CONFIRMATION_REQUIRED = "confirmation_required"
MFA_FIRST = "step_up_mfa_setup"
# The refusal's variant for a person who confirms at the provider.
SSO = "sso"
# What a confirmation was: named on auth.confirmed, under every setting.
PASSWORD = "password"
CODE = "code"
KEY = "key"
PROVIDER = "provider"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Failed confirmations in a row that end the sign-in.
MAX_FAILURES = 3

BAD_CODE = (
    "That code didn't work. Check the app and try again; codes change every 30 seconds."
)
BAD_PASSWORD = "That password didn't work. Try again."
CONFIRM_AT_PROVIDER = "Confirm it's you at your SSO provider first."
BAD_PROOF = "Your confirmation at your SSO provider has expired. Confirm there again."
# Never says which of the two was right.
BAD_STRONG = "That didn't work. Check your password and code, then try again."


def window() -> timedelta:
    """How long a Confirm-it's-you lasts.

    A nonzero privileged idle limit is the window, up to 12 hours. At 0 (no
    idle limit) the window is 10 minutes: turning the idle limit off neither
    switches off nor tightens this check. ``window_end``, the refusal's
    ``window_minutes``, the recovery-code step-down and the confirmation's
    ``window_ends_at`` all read this.
    """
    minutes = session_policy.policy().idle_privileged_minutes or 10
    return timedelta(minutes=minutes)


def window_end(confirmed_at: Optional[datetime]) -> Optional[datetime]:
    """When the window that ``confirmed_at`` opened closes, if it is still open."""
    end = confirmed_at + window() if confirmed_at else None
    return end if end and end > utcnow() else None


def open_window(session: Session, sign_in_id: Optional[str]) -> bool:
    """Start or restart the window, and forget earlier failures. False: no open sign-in."""
    if not sign_in_id:
        return False
    updated = (
        session.query(SignIn)
        .filter(SignIn.sign_in_id == sign_in_id, SignIn.ended_at.is_(None))
        .update(
            {SignIn.confirmed_at: utcnow(), SignIn.confirm_failures: 0},
            synchronize_session=False,
        )
    )
    return updated > 0


def non_sso_now_refused(user: User, auth_method: Optional[str]) -> bool:
    """Whether this open sign-in must end: a new one for the same account, made
    without SSO, would be refused now.

    That is login's rule. Require single sign-on is on, the sign-in did not come
    through it, and the account is neither break-glass nor a service account.
    """
    if auth_method == sign_ins.SSO:
        return False
    if user.break_glass_at is not None:
        return False
    if user.service_account is True:
        return False
    return sso.required()


def end_non_sso(session: Session, user: User, sign_in_id: Optional[str]) -> None:
    """End this sign-in and 401 when ``non_sso_now_refused`` is true.

    Before any password or code is read, and it counts no failure. Its own
    transaction: the 401 rolls the request's back. No trail entry: turning
    Require single sign-on on is already recorded.
    """
    if not sign_in_id:
        return
    method = (
        session.query(SignIn.auth_method)
        .filter(
            SignIn.sign_in_id == sign_in_id,
            SignIn.principal_id == user.principal_id,
            SignIn.ended_at.is_(None),
        )
        .scalar()
    )
    if method is None or not non_sso_now_refused(user, method):
        return
    if sign_ins.end(sign_in_id, sign_ins.SSO_REQUIRED):
        raise _ended(sign_ins.SSO_REQUIRED)


def provider_only(session: Session, user: Optional[User]) -> bool:
    """No Vigil factor, and signs in only through the provider: the provider, and
    never the password, opens this person's window (#4285, #4756)."""
    return user is not None and not user.mfa_enabled and sso.sso_only(session, user)


def at_provider(session: Session, principal_id: str, sign_in_id: str) -> bool:
    """Whether this person confirms at the provider for this open sign-in of theirs:
    ``provider_only``, and the sign-in itself came through the provider. A password
    sign-in left open when Require SSO was turned on is not one."""
    user = session.query(User).filter(User.principal_id == principal_id).first()
    method = (
        session.query(SignIn.auth_method)
        .filter(
            SignIn.sign_in_id == sign_in_id,
            SignIn.principal_id == principal_id,
            SignIn.ended_at.is_(None),
        )
        .scalar()
    )
    return method == sign_ins.SSO and provider_only(session, user)


def confirmed(
    session: Session,
    principal_id: str,
    sign_in_id: Optional[str],
    source: Optional[str],
    strong: bool = False,
    window: bool = True,
    factor: Optional[str] = None,
) -> bool:
    """A confirmation that held: open the window and put it on the trail. False,
    and nothing recorded, when the sign-in has ended meanwhile. ``window=False``
    only records it: the factor given cannot open the window. ``factor`` is
    password, code, key or provider."""
    if window and sign_in_id and not open_window(session, sign_in_id):
        return False
    trail_events.record(
        session,
        "auth.confirmed",
        {"source_ip": source},
        actor=principal_id,
        principal=principal_id,
        sign_in=sign_in_id,
        strong=strong or None,
        factor=factor,
    )
    return True


def _refusal(code: str, **fields) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, detail={"code": code, **fields}
    )


def check(request: Request, user_id: str, action: str) -> None:
    """403 unless the caller is not a person, or is inside the window.

    ``action`` names the change in the dialog's title ("change settings").
    A person with no MFA is sent to set it up, unless Require MFA is off: then
    they confirm with their password. The setting is read here, each request,
    the same way sign-in reads it (``session_policy.policy``); an unreadable
    row is the default, which is not off. One who signs in only through the
    provider (``sso.sso_only``) confirms there (#4285).
    """
    actor = getattr(request.state, "vigil_http_actor", None)
    claims = getattr(request.state, "access_claims", None)
    sid = claims.get("sid") if isinstance(claims, dict) else None
    # Dev mode has no actor; a machine or a bearer-less caller has no person to ask.
    if actor is None or not actor.person or not sid:
        return
    with unit_of_work() as session:
        user = session.query(User).filter(User.user_id == user_id).first()
        confirmed, method = (
            session.query(SignIn.confirmed_at, SignIn.auth_method)
            .filter(SignIn.sign_in_id == sid)
            .first()
        ) or (None, None)
        # A break-glass sign-in spent a code to get in; asking again would spend more.
        if method == sign_ins.BREAK_GLASS:
            return
        mfa = bool(user and user.mfa_enabled)
        only_provider = provider_only(session, user)
        # Only a sign-in through the provider: an open password sign-in stays
        # held to the two-step rule it had before Require SSO.
        via_provider = method == sign_ins.SSO and only_provider
        # Before the window, so raising the setting inside one still sends a
        # person with no factor to set one up.
        require_mfa = session_policy.policy().require_mfa
        # Under off, confirm() will not take a password from someone who can
        # sign in only at the provider. Ask for that, not a password.
        if require_mfa == "off" and only_provider:
            via_provider = True
    if not mfa and not via_provider and require_mfa != "off":
        raise _refusal(
            MFA_FIRST,
            action=action,
            message="Set up two-step sign-in before changes like this",
        )
    if window_end(confirmed) is None:
        raise _refusal(
            CONFIRMATION_REQUIRED,
            variant=SSO if via_provider else "one_factor",
            action=action,
            **({"provider": _provider_name()} if via_provider else {}),
            # What this request will accept, so the dialog does not keep a
            # factor the sign-in loaded and that has since been cleared.
            **({} if via_provider else {"accepts": CODE if mfa else PASSWORD}),
            window_minutes=int(window().total_seconds() // 60),
            confirmed_at=confirmed.isoformat() if confirmed else None,
        )


def _provider_name() -> Optional[str]:
    """The provider's host, for the dialog's "Confirm at …"."""
    from core.auth import sso_settings

    issuer = sso_settings.settings().issuer
    return urlparse(issuer).hostname if issuer else None


def gate(permission: str, action: str) -> Depends:
    """A route dependency that asks before the handler runs, once the caller holds
    ``permission``. For a handler that records its own 403s (those must not log
    "confirm" as a denial); ``deny_unless`` asks for the rest. A caller without the
    permission falls through to the handler's own 403.
    """
    from core.auth.permissions import missing_permission

    def _check(request: Request, user: User = Depends(get_current_user)) -> None:
        if request.method not in SAFE_METHODS and (
            missing_permission(user.user_id, permission) is None
        ):
            check(request, user.user_id, action)

    return Depends(_check)


def _ended(reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sign-in has ended",
        headers={
            "WWW-Authenticate": "Bearer",
            sign_ins.END_REASON_HEADER: reason,
        },
    )


def _fail(sign_in_id: str, principal_id: str, source: Optional[str]) -> bool:
    """Count a failed confirmation and end the sign-in on the third. True if it ended.

    Its own transaction: the 400 the caller raises rolls the request's back.
    """
    with unit_of_work() as session:
        row = session.get(
            SignIn, sign_in_id, with_for_update=True, populate_existing=True
        )
        if row is None or row.ended_at is not None:
            return True
        row.confirm_failures = (row.confirm_failures or 0) + 1
        trail_events.record(
            session,
            "auth.confirmation_failed",
            {"source_ip": source},
            actor=principal_id,
            principal=principal_id,
            sign_in=sign_in_id,
            failures=row.confirm_failures,
        )
        if row.confirm_failures < MAX_FAILURES:
            return False
        sign_ins.end(sign_in_id, sign_ins.TOO_MANY_CONFIRMATIONS, session)
        trail_events.record(
            session,
            "auth.sign_in_ended",
            {"source_ip": source},
            actor=principal_id,
            principal=principal_id,
            sign_in=sign_in_id,
            end_reason=sign_ins.TOO_MANY_CONFIRMATIONS,
        )
        return True


def confirm(
    user: User,
    session: Session,
    sign_in_id: Optional[str],
    *,
    password: Optional[str] = None,
    code: Optional[str] = None,
    assertion: Optional[dict] = None,
    challenge: Optional[str] = None,
    strong: bool = False,
    signed_in: bool = False,
    source: Optional[str] = None,
    settle: bool = True,
    method_change: bool = False,
    sso_proof: Optional[str] = None,
) -> str:
    """Check it is still the person, or raise. Returns the factor that held.

    One-factor: the authenticator or recovery code with MFA, the password
    without it. Strong: the password and, with MFA, a current code, on every call
    whatever the window. A person with security keys may answer with an
    ``assertion`` (and the ``challenge`` it signed) in place of the code. A success opens the window; a failure counts, and the
    third ends the sign-in (401). 400, not 401, for a wrong answer: a 401 would
    make the web client refresh and retry. An empty field is asked for, not counted.
    ``signed_in``: the sign-in itself gave the code (a key-only person's recovery
    sign-in while keys are unavailable), so no code is asked for.
    ``settle=False`` only checks the factor. The caller opens the window, and only
    when ``auth.confirmed`` was appended: ending one's own sign-ins must not
    widen the sign-in when that entry can only be held.
    ``method_change``: adding or changing a sign-in method. For a person who signs
    in only through the provider, ``sso_proof`` from confirming there stands in for
    the password, and a password is never taken (#4756).
    A sign-in a new password sign-in would now be refused ends first, before any
    of this, and counts nothing (#4841).
    """
    end_non_sso(session, user, sign_in_id)
    needs_password = strong or not user.mfa_enabled
    needs_code = user.mfa_enabled and not signed_in
    via_proof = method_change and needs_password and sso.sso_only(session, user)
    if via_proof and not sso_proof:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, CONFIRM_AT_PROVIDER)
    if needs_password and not via_proof and not password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter your password.")
    if needs_code and not code and not assertion:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, BAD_CODE)

    problem = None
    if via_proof and not sso.proof_holds(sso_proof, sign_in_id, user.principal_id):
        problem = BAD_PROOF
    elif (
        needs_password
        and not via_proof
        and not AuthService.verify_password(password, user.password_hash)
    ):
        problem = BAD_STRONG if strong else BAD_PASSWORD
    # The code is only spent once the password (or the proof) is right.
    elif needs_code and assertion:
        try:
            security_keys.verify_assertion(
                session, user, assertion, challenge, security_keys.CONFIRM
            )
        except security_keys.KeyRefused as exc:
            problem = BAD_STRONG if strong else str(exc)
    elif needs_code and not AuthService.verify_mfa_code(user.user_id, code, session):
        problem = BAD_STRONG if strong else BAD_CODE

    kind = _factor(via_proof=via_proof, assertion=assertion, needs_code=needs_code)
    if problem is None:
        if settle:
            # The password checks this one action, but never opens the window
            # of a person who confirms at the provider. Under Require MFA off
            # it does open the window of a person with no factor.
            confirmed(
                session,
                user.principal_id,
                sign_in_id,
                source,
                strong=strong,
                window=not provider_only(session, user),
                factor=kind,
            )
        return kind
    _refuse(user, sign_in_id, source, problem)


def _factor(*, via_proof: bool, assertion: Optional[dict], needs_code: bool) -> str:
    """The kind of factor that held: password, code, key or provider."""
    if via_proof:
        return PROVIDER
    if assertion:
        return KEY
    if needs_code:
        return CODE
    return PASSWORD


def _refuse(user: User, sign_in_id: Optional[str], source, problem: str) -> NoReturn:
    if sign_in_id and _fail(sign_in_id, user.principal_id, source):
        raise _ended(sign_ins.TOO_MANY_CONFIRMATIONS)
    raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
