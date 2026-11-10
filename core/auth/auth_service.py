"""
Authentication Service - User authentication and authorization.

Handles password hashing, JWT generation/validation, MFA, and session management.
"""

import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import jwt
import pyotp
from cryptography.fernet import MultiFernet
from sqlalchemy import update
from sqlalchemy.orm import Session

from core.auth import key_ring, password_hash, principals, session_policy
from core.auth.identity import claim_sign_in_names, identifier_in_use, matching_accounts
from core.config import get_settings
from core.exceptions import default_on_error
from core.storage.models import Principal, Role, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

# A person's own act (decision 9). Never held by a service account, token, or agent.
PERSON_ONLY = frozenset({"audit.forward", "audit.manage"})


def _is_person(user_id: str, session: Optional[Session]) -> bool:
    """Whether that user's principal is a person. No row is not one."""
    with unit_of_work(session) as session:
        principal_id = (
            session.query(User.principal_id).filter(User.user_id == user_id).scalar()
        )
        if not principal_id:
            return False
        kind = (
            session.query(Principal.kind)
            .filter(Principal.principal_id == principal_id)
            .scalar()
        )
        return kind == principals.PERSON


# Fernet tokens are urlsafe base64 of a version byte + timestamp, so they start
# with "gAAAAA". Lowercase "g" is not base32, so legacy plaintext never matches.
_FERNET_TOKEN_PREFIX = "gAAAAA"
_UNDECRYPTABLE_LOG_INTERVAL = 60.0
_last_undecryptable_log = float("-inf")


class MFASecretDecryptionError(Exception):
    """A stored MFA secret is encrypted but not under a key in the ring."""


def _is_dev_mode() -> bool:
    return get_settings().dev_mode


def _refuse_dev_mode_in_production() -> None:
    # The auth bypass is never right on an install declared production; refuse
    # at import so every process that loads auth fails closed.
    settings = get_settings()
    if settings.dev_mode and settings.environment.strip().lower() == "production":
        raise RuntimeError(
            "DEV_MODE=true bypasses authentication and is refused when "
            "ENVIRONMENT=production. Unset DEV_MODE (or set it to false), or "
            "unset ENVIRONMENT if this is not a production install."
        )


_refuse_dev_mode_in_production()

# Fails closed at import when the secret that wraps the key ring is unset
# (DEV_MODE generates one); every process that loads auth gets the check.
key_ring.wrapping_secret()

# JWT Configuration
JWT_ALGORITHM = "HS256"
JWT_ACCESS_EXPIRATION_MINUTES = get_settings().jwt_access_expiration_minutes
JWT_REFRESH_EXPIRATION_DAYS = get_settings().jwt_refresh_expiration_days

# Old codes were 8 hex characters, new ones 16
_RECOVERY_CODE = re.compile(r"[0-9A-F]{8}|[0-9A-F]{16}")

# What ``verify_mfa_code`` accepted: the app, a current recovery code, or a
# bcrypt-era one (#4177, which only upgrades the set).
CODE_APP = "app"
CODE_RECOVERY = "recovery"
CODE_OLD_RECOVERY = "old_recovery"


def _is_service_account(user: User) -> bool:
    """Only an explicit True exempts from lockout; anything else fails closed."""
    return user.service_account is True


class AccountLockedError(Exception):
    """Raised when authentication is refused because the account is locked.
    ``locked_until`` is None for a lock that lasts until an Unlock or a reset link."""

    def __init__(self, locked_until: Optional[datetime]):
        self.locked_until = locked_until
        super().__init__(
            f"Account locked until {locked_until.isoformat()}"
            if locked_until
            else "Account locked until unlocked"
        )


def _account_is_locked(user: User) -> bool:
    """A timed lock holds until ``locked_until``; one with only ``locked_at`` set
    holds until an Unlock or a reset link clears it."""
    if _is_service_account(user):
        return False
    if user.locked_until is not None:
        return user.locked_until > utcnow()
    return user.locked_at is not None


def clear_lock(user: User) -> None:
    """The count and the lock end. Only a complete sign-in, an Unlock or a reset
    link calls this."""
    user.failed_login_count = 0
    user.locked_until = user.locked_at = user.locked_from = None


def raise_if_locked(user: User) -> None:
    if _account_is_locked(user):
        logger.warning("Refused, account locked: %s", user.username)
        raise AccountLockedError(user.locked_until)


def password_matches_any(plaintext: str, hashes) -> bool:
    """Return True if `plaintext` matches any hash in the iterable (either scheme)."""
    return password_hash.find(plaintext, list(hashes or [])) is not None


def managers(person: str) -> list[str]:
    """Principal ids of active people, other than this one, who hold users.write."""
    with unit_of_work() as session:
        users = (
            session.query(User.user_id, User.principal_id)
            .filter(User.is_active.is_(True), User.service_account.isnot(True))
            .all()
        )
        return [
            principal
            for user_id, principal in users
            if principal != person
            and AuthService.check_permission(user_id, "users.write", session=session)
        ]


class AuthService:
    """Service for user authentication and authorization."""

    @staticmethod
    def hash_password(password: str) -> str:
        """Hash a password with PBKDF2-HMAC-SHA-256 (core.auth.password_hash)."""
        return password_hash.hash(password)

    @staticmethod
    @default_on_error(False)
    def verify_password(password: str, stored_hash: str) -> bool:
        """Verify a password against its hash (PBKDF2, or a bcrypt from before)."""
        return password_hash.verify(password, stored_hash)

    @staticmethod
    def generate_jwt_token(
        user: User,
        token_type: str = "access",
        sid: Optional[str] = None,
        generation: Optional[int] = None,
        not_after: Optional[datetime] = None,
    ) -> str:
        """Generate a JWT token for a user.

        ``sid`` names the sign-in the token belongs to; every token gets a
        fresh ``jti`` either way. A refresh token carries the sign-in's refresh
        ``generation``. ``not_after`` caps the expiry: nothing outlives the
        sign-in's absolute limit."""
        expiration = (
            timedelta(minutes=JWT_ACCESS_EXPIRATION_MINUTES)
            if token_type == "access"
            else timedelta(days=JWT_REFRESH_EXPIRATION_DAYS)
        )

        now = utcnow()
        payload = {
            "user_id": user.user_id,
            "username": user.username,
            "email": user.email,
            "role_id": user.role_id,
            "token_type": token_type,
            "jti": uuid.uuid4().hex,
            "exp": min(now + expiration, not_after) if not_after else now + expiration,
            "iat": now,
        }

        if sid:
            payload["sid"] = sid
        if generation is not None:
            payload["gen"] = generation

        return key_ring.sign("signin", payload, JWT_ALGORITHM)

    @staticmethod
    def verify_jwt_token(token: str) -> Optional[Dict[str, Any]]:
        """
        Verify and decode a JWT token.

        Args:
            token: JWT token string

        Returns:
            Decoded payload or None if invalid
        """
        try:
            payload = jwt.decode(
                token,
                key_ring.token_key("signin", token),
                algorithms=[JWT_ALGORITHM],
            )
            return payload
        except jwt.ExpiredSignatureError:
            logger.warning("JWT token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            return None

    @staticmethod
    def expired_refresh_payload(token: str) -> Optional[Dict[str, Any]]:
        """The claims of a validly signed refresh token whose expiry has passed,
        so the sign-in it carries can be ended. None for anything else."""
        try:
            payload = jwt.decode(
                token,
                key_ring.token_key("signin", token),
                algorithms=[JWT_ALGORITHM],
                options={"verify_exp": False},
            )
        except jwt.InvalidTokenError:
            return None
        exp = payload.get("exp")
        if payload.get("token_type") != "refresh" or exp is None:
            return None
        return payload if exp <= time.time() else None

    @staticmethod
    def authenticate_user(
        username_or_email: str,
        password: str,
        session: Optional[Session] = None,
        refusal: Optional[dict] = None,
        source: Optional[str] = None,
    ) -> Optional[User]:
        """The user, or None (or AccountLockedError). On a refusal, ``refusal``
        gets its ``reason`` and, for a known account, its ``principal``."""
        refusal = {} if refusal is None else refusal
        # The unit of work must see any failure, so it sits inside the try —
        # the handlers below swallow the exception and would otherwise let a
        # failed attempt commit.
        try:
            with unit_of_work(session) as session:
                matches = matching_accounts(session, username_or_email)
                if len(matches) != 1:
                    # Two rows share the string. Picking either one checks the
                    # password against the wrong hash and writes the failure
                    # against the wrong account. Neither account is named.
                    if len(matches) > 1:
                        logger.warning(
                            "Login identifier matched more than one account: %s",
                            ", ".join(sorted(user.username for user in matches)),
                        )
                        refusal["reason"] = "ambiguous"
                    else:
                        logger.warning("Login attempt for unknown identifier")
                        refusal["reason"] = "unknown"
                    return None
                user = matches[0]
                refusal["principal"] = user.principal_id

                if not user.is_active:
                    logger.warning(
                        "Login attempt for inactive account: %s", user.username
                    )
                    refusal["reason"] = "inactive"
                    return None

                # Reject while locked. Lockout is authoritative even over a
                # correct password — otherwise an attacker who eventually
                # guesses right would bypass the wait.
                now = utcnow()
                if _account_is_locked(user):
                    refusal["reason"] = "locked"
                raise_if_locked(user)

                # Verify password
                if not AuthService.verify_password(password, user.password_hash):
                    AuthService._record_failed_attempt(user.user_id, now, source)
                    logger.warning("Invalid password for user: %s", user.username)
                    refusal["reason"] = "bad_password"
                    return None

                # A bcrypt hash is replaced now that the password is known. Not a
                # change: no history entry, password_changed_at untouched.
                if password_hash.needs_rewrite(user.password_hash):
                    # Only over the hash just verified: a reset or change that
                    # committed meanwhile is not undone.
                    session.execute(
                        update(User)
                        .where(
                            User.user_id == user.user_id,
                            User.password_hash == user.password_hash,
                        )
                        .values(password_hash=password_hash.hash(password))
                        .execution_options(synchronize_session=False)
                    )
                    session.expire(user, ["password_hash"])

                # Success — reset lockout state and update session tracking
                clear_lock(user)
                user.last_login = now
                user.login_count += 1

                logger.info(f"User authenticated successfully: {username_or_email}")
                return user

        except AccountLockedError:
            raise
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            refusal["reason"] = "error"
            return None

    @staticmethod
    def _record_failed_attempt(
        user_id: str, now: datetime, source: Optional[str] = None
    ) -> None:
        """Persist one failed login attempt in its own transaction, and lock the
        account when the policy's threshold is reached.

        Deliberately does not join the caller's transaction: the login endpoint
        answers a failed attempt with 401, which rolls that transaction back. If
        this bookkeeping went with it the counter would never survive, the
        threshold would never trip, and the lockout would silently do nothing.
        """
        # The policy is read at each failure, so a saved change applies at once.
        policy = session_policy.policy()
        locked = None
        with unit_of_work() as session:
            # Row-locked: concurrent failures count one by one, one sets the lock.
            user = (
                session.query(User)
                .filter(User.user_id == user_id)
                .with_for_update()
                .first()
            )
            if not user:
                return
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if _is_service_account(user):
                # Never locked (D2-17): a lock would let anyone who learns the
                # name blind Medic's gateway. Safe only because the password is
                # long, random and held by the gateway alone. Still counted, and
                # logged once at the threshold so brute force stays visible.
                if user.failed_login_count == policy.lockout_threshold:
                    logger.warning(
                        "Service account %s reached %d failed logins; "
                        "service accounts are not locked out",
                        user.username,
                        user.failed_login_count,
                    )
                return
            if (
                user.failed_login_count >= policy.lockout_threshold
                and not _account_is_locked(user)
            ):
                # The count is not reset when a timed lock ends: each failure
                # after it locks again, and LOCK_UNTIL_UNLOCKED of them in a
                # row lock until unlocked.
                timed = (
                    policy.lockout_timed
                    and user.failed_login_count < session_policy.LOCK_UNTIL_UNLOCKED
                )
                user.locked_until = (
                    now + timedelta(minutes=policy.lockout_minutes) if timed else None
                )
                user.locked_at, user.locked_from = now, source
                locked = (
                    user.principal_id,
                    user.username,
                    user.failed_login_count,
                    now,
                    user.locked_until,
                    source,
                )
                logger.warning(
                    "Account locked after %d failed attempts: %s",
                    user.failed_login_count,
                    user.username,
                )
        if locked:
            AuthService._tell_locked(*locked)

    @staticmethod
    def _tell_locked(
        principal_id: str,
        username: str,
        failures: int,
        when: datetime,
        until: Optional[datetime],
        source: Optional[str],
    ) -> None:
        """One notice to each manager and one to the person, who sees it at their
        next sign-in. A lock ends no sign-in, so the notices are what tell anyone."""
        from core.platform.notices import notify

        where = f"from {source}" if source else "from an unknown address"
        at = f"{when:%Y-%m-%d %H:%M} UTC"
        ends = (
            f"It ends on its own at {until:%Y-%m-%d %H:%M} UTC."
            if until
            else "It lasts until an administrator unlocks the account or the "
            "person uses a password reset link."
        )
        try:
            to_managers = managers(principal_id)
        except Exception:  # noqa: BLE001 - the lock stands, so does the person's notice
            logger.exception("could not find the managers to tell about a lock")
            to_managers = []
        notices = [
            (
                principal,
                "account_locked",
                f"Account locked: {username}",
                f"{username} was locked at {at} after {failures} failed sign-ins "
                f"{where}. {ends} Open sign-ins were not ended. Unlock it in "
                "Settings once you have confirmed it is them.",
            )
            for principal in to_managers
        ]
        notices.append(
            (
                principal_id,
                "account_locked",
                "Your account was locked",
                f"Your account was locked at {at} after {failures} failed sign-ins "
                f"{where}. {ends} If this was not you, tell an administrator.",
            )
        )
        for principal, kind, title, message in notices:
            try:
                notify(
                    principal,
                    kind,
                    title,
                    message,
                    link=(
                        "/settings?section=users" if principal != principal_id else None
                    ),
                    priority="high",
                )
            except Exception:  # noqa: BLE001 - one missed notice must not hide the rest
                logger.exception("could not send a lock notice")

    @staticmethod
    def _count_mfa_failure(
        user_id: str, username: str, source: Optional[str] = None
    ) -> None:
        """Record a wrong code after the row lock has been released."""
        AuthService._record_failed_attempt(user_id, utcnow(), source)
        logger.warning("Invalid MFA code for user: %s", username)

    @staticmethod
    @default_on_error(None)
    def setup_mfa(user_id: str, session: Optional[Session] = None) -> Optional[str]:
        with unit_of_work(session) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                return None

            secret = pyotp.random_base32()
            user.mfa_secret = AuthService._encrypt_mfa_secret(secret)
            user.mfa_enabled = False
            # Clear any stale codes from a prior setup attempt; fresh codes
            # are issued at enable time.
            user.mfa_recovery_codes = []
            user.mfa_last_totp_step = None  # the old secret's steps don't apply

            logger.info(f"MFA setup initiated for user: {user.username}")
            return secret

    @staticmethod
    @default_on_error(None)
    def enable_mfa(
        user_id: str, code: str, session: Optional[Session] = None
    ) -> Optional[List[str]]:
        """Verify the first TOTP code and enable MFA.

        On the enabling transition, generates one-time recovery codes and
        returns the plaintext (shown once, cannot be retrieved again).

        Returns:
            - list of recovery codes on successful enable
            - [] if the code is valid but MFA was already enabled (no reissue)
            - None if there is no pending secret or the code is invalid
        """
        failed = False
        result: Optional[List[str]] = None
        with unit_of_work(session) as session:
            nested = session.begin_nested()
            try:
                user = AuthService._lock_user(session, user_id)
                if not user or not user.mfa_secret or _account_is_locked(user):
                    pass
                else:
                    try:
                        valid = AuthService._accept_totp(user, code)
                    except MFASecretDecryptionError:
                        valid = None  # already logged at ERROR with the cause
                    if valid is None:
                        pass
                    elif not valid:
                        # A confirmation: a wrong code is refused, not counted.
                        failed = True
                    elif user.mfa_enabled:
                        # Already enabled — codes were issued at enable time,
                        # don't silently reissue. Use regenerate to rotate.
                        result = []
                    else:
                        user.mfa_enabled = True
                        codes, user.mfa_recovery_codes = (
                            AuthService._generate_recovery_codes()
                        )
                        logger.info(f"MFA enabled for user: {user.username}")
                        result = codes
            finally:
                if failed:
                    nested.rollback()
                elif nested.is_active:
                    nested.commit()
        return result

    @staticmethod
    @default_on_error(None)
    def get_mfa_recovery_codes(
        user_id: str, session: Optional[Session] = None
    ) -> Optional[List[str]]:
        """Replace a user's recovery codes. The caller has run the strong check
        (``step_up.confirm``), so no code is asked for here.

        Returns plaintext codes (caller displays them once), or None if MFA
        is not enabled.
        """
        with unit_of_work(session) as session:
            user = AuthService._lock_user(session, user_id)
            if not user or not user.mfa_enabled:
                return None

            codes, user.mfa_recovery_codes = AuthService._generate_recovery_codes()
            return codes

    @staticmethod
    @default_on_error(None)
    def verify_mfa_code(
        user_id: str,
        code: str,
        session: Optional[Session] = None,
        *,
        count_failures: bool = False,
        source: Optional[str] = None,
    ) -> Optional[str]:
        """Verify a TOTP code or a one-time recovery code.

        Returns which kind was accepted (``CODE_APP``, ``CODE_RECOVERY`` for a
        current-scheme recovery code, ``CODE_OLD_RECOVERY`` for a bcrypt-era one),
        or None: truthy when it passed, so a caller that only asks "did it" still can.

        Enabling MFA is handled by enable_mfa(); this method assumes MFA is
        already enabled and only validates the supplied code. Only the login
        step passes ``count_failures``: a wrong code at a confirmation
        (recovery codes, disabling MFA, a step-up) does not count toward a lock.
        """
        failed: Optional[tuple[str, str]] = None
        accepted: Optional[str] = None
        with unit_of_work(session) as session:
            nested = session.begin_nested()
            try:
                # Locked so two requests carrying the same code are serialised:
                # the second sees the consumed recovery code / stored TOTP step.
                user = AuthService._lock_user(session, user_id)
                if not user or not user.mfa_secret or _account_is_locked(user):
                    return None

                # Try TOTP first. An undecryptable secret must not block recovery
                # codes: they are hashes and do not depend on the key.
                try:
                    totp_ok = AuthService._accept_totp(user, code)
                except MFASecretDecryptionError:
                    totp_ok = False  # already logged at ERROR with the cause
                if totp_ok:
                    accepted = CODE_APP
                else:
                    recovery_codes = list(user.mfa_recovery_codes or [])
                    candidate = code.strip().upper()
                    # Only a code-shaped string costs hashes; a wrong 6-digit
                    # TOTP is refused without one.
                    # One PBKDF2 derivation covers the whole set (shared salt).
                    found = (
                        password_hash.find(candidate, recovery_codes)
                        if _RECOVERY_CODE.fullmatch(candidate)
                        else None
                    )
                    if found is not None:
                        accepted = (
                            CODE_OLD_RECOVERY
                            if password_hash.needs_rewrite(recovery_codes[found])
                            else CODE_RECOVERY
                        )
                        recovery_codes.pop(found)
                        user.mfa_recovery_codes = recovery_codes
                        logger.info(
                            "Recovery code used for user: %s (%d remaining)",
                            user.username,
                            len(recovery_codes),
                        )
                    if not accepted:
                        failed = (user.user_id, user.username)
            finally:
                if failed is not None:
                    nested.rollback()
                elif nested.is_active:
                    nested.commit()
        if failed and count_failures:
            # The savepoint is already closed; count on a fresh transaction.
            AuthService._count_mfa_failure(failed[0], failed[1], source)
        return accepted

    @staticmethod
    @default_on_error(False)
    def disable_mfa(user_id: str, session: Optional[Session] = None) -> bool:
        """Turn MFA off. The caller has run the strong check (``step_up.confirm``).

        Clears the secret and any remaining recovery codes. Returns False
        (leaving MFA untouched) if MFA is not enabled.
        """
        with unit_of_work(session) as session:
            user = AuthService._lock_user(session, user_id)
            if not user or not user.mfa_enabled:
                return False
            AuthService.clear_mfa(user)
            logger.info(f"MFA disabled for user: {user.username}")
            return True

    @staticmethod
    def clear_mfa(user: User) -> None:
        """Remove every method: the app, its steps and the remaining codes."""
        user.mfa_enabled = False
        user.mfa_secret = None
        user.mfa_recovery_codes = []
        user.mfa_last_totp_step = None

    @staticmethod
    def _generate_recovery_codes() -> tuple[list[str], list[str]]:
        """Return (plaintext, hashed) lists of 10 one-time recovery codes."""
        codes = [secrets.token_hex(8).upper() for _ in range(10)]  # 64 bits each
        return codes, password_hash.hash_set(codes)

    @staticmethod
    def upgrade_recovery_codes(user: User) -> Optional[List[str]]:
        """After a good MFA step: if the stored set is from the bcrypt era, void
        it and return a fresh plaintext set. The caller holds the user's row
        lock, so an old code can't be spent twice. None when there is nothing
        to replace."""
        if not any(
            password_hash.needs_rewrite(h) for h in user.mfa_recovery_codes or []
        ):
            return None
        codes, user.mfa_recovery_codes = AuthService._generate_recovery_codes()
        logger.info("Recovery codes replaced after upgrade for user: %s", user.username)
        return codes

    @staticmethod
    def _fernet(force: bool = False) -> MultiFernet:
        """The ring's MFA keys: encrypts under the current, decrypts under any."""
        return key_ring.encryptor("mfa", force=force)

    @staticmethod
    def _encrypt_mfa_secret(secret: str) -> str:
        """Encrypt an MFA secret at rest under the ring's current MFA key."""
        return AuthService._fernet().encrypt(secret.encode()).decode()

    @staticmethod
    def _decrypt_mfa_secret(encrypted: str) -> str:
        """Decrypt an MFA secret; fall back to plaintext for pre-migration rows.

        Raises:
            MFASecretDecryptionError: the value is a Fernet token that the
                ring's MFA keys cannot decrypt.
        """
        fernet = AuthService._fernet()  # an unreadable ring raises, never plaintext
        try:
            return fernet.decrypt(encrypted.encode()).decode()
        except Exception as exc:
            if not encrypted.startswith(_FERNET_TOKEN_PREFIX):
                return encrypted  # legacy plaintext (pre-migration rows)
            try:  # a rotation on another replica: read the ring again
                return (
                    AuthService._fernet(force=True).decrypt(encrypted.encode()).decode()
                )
            except Exception:  # noqa: BLE001 - reported below as undecryptable
                pass
            AuthService._log_undecryptable_secret()
            raise MFASecretDecryptionError(
                "MFA secret cannot be decrypted with the ring's MFA keys"
            ) from exc

    @staticmethod
    def _log_undecryptable_secret() -> None:
        """Log the key mismatch at ERROR, at most once per interval."""
        global _last_undecryptable_log
        now = time.monotonic()
        if now - _last_undecryptable_log < _UNDECRYPTABLE_LOG_INTERVAL:
            return
        _last_undecryptable_log = now
        logger.error(
            "MFA secret cannot be decrypted with the ring's MFA keys; "
            "the ring has changed since MFA was set up. TOTP codes are rejected "
            "until the original ring is restored; recovery codes still work."
        )

    @staticmethod
    def _lock_user(session: Session, user_id: str) -> Optional[User]:
        """Load a user row FOR UPDATE, held until the unit of work commits.

        populate_existing() refreshes a User already in the session (login
        loads it in authenticate_user); otherwise the lock is taken but the
        stale recovery codes and TOTP step from the earlier read are kept.
        """
        return (
            session.query(User)
            .filter(User.user_id == user_id)
            .populate_existing()
            .with_for_update()
            .first()
        )

    @staticmethod
    def _accept_totp(user: User, code: str) -> bool:
        """Accept a TOTP code once: its step must be later than the last accepted."""
        step = AuthService._verify_totp(user.mfa_secret, code)
        if step is None:
            return False
        if user.mfa_last_totp_step is not None and step <= user.mfa_last_totp_step:
            logger.warning("Replayed TOTP code refused for user: %s", user.username)
            return False
        user.mfa_last_totp_step = step
        return True

    @staticmethod
    def _verify_totp(encrypted_secret: str, code: str) -> Optional[int]:
        """Return the time step (±1) a TOTP code matches, or None."""
        totp = pyotp.TOTP(AuthService._decrypt_mfa_secret(encrypted_secret))
        current = int(time.time()) // totp.interval
        # Newest first, so a code valid in two steps records the later one.
        for step in (current + 1, current, current - 1):
            # strings_equal is constant-time (hmac.compare_digest underneath)
            if pyotp.utils.strings_equal(str(code), totp.generate_otp(step)):
                return step
        return None

    @staticmethod
    def get_mfa_qr_uri(
        user_id: str, session: Optional[Session] = None
    ) -> Optional[str]:
        """
        Get MFA QR code URI for a user.

        Args:
            user_id: User ID
            session: Database session (optional)

        Returns:
            QR code URI or None
        """
        with unit_of_work(session) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user or not user.mfa_secret:
                return None

            try:
                decrypted_secret = AuthService._decrypt_mfa_secret(user.mfa_secret)
            except MFASecretDecryptionError:
                return None  # already logged at ERROR with the cause
            totp = pyotp.TOTP(decrypted_secret)
            uri = totp.provisioning_uri(name=user.email, issuer_name="Vigil SOC")
            return uri

    @staticmethod
    def check_permission(
        user_id: str, permission: str, session: Optional[Session] = None
    ) -> bool:
        """
        Check if a user has a specific permission.

        Args:
            user_id: User ID
            permission: Permission string (e.g., "cases.write")
            session: Database session (optional)

        Returns:
            True if user has permission
        """
        # audit.forward and audit.manage are a person's own act. Dev mode grants
        # every role permission, and this still refuses a non-person, including
        # a dev user with no principal row.
        if permission in PERSON_ONLY and not _is_person(user_id, session):
            return False

        # DEV MODE: built-in Admin, through the catalog. A name it does not
        # list is denied. The person-only refusal above still wins.
        if _is_dev_mode():
            from core.auth.permissions import CATALOG

            return bool(CATALOG["builtins"]["role-admin"].get(permission))

        with unit_of_work(session) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user or not user.is_active:
                return False

            role = session.query(Role).filter(Role.role_id == user.role_id).first()
            if not role:
                return False

            # Check permission in role's permissions JSONB
            return role.permissions.get(permission, False)

    @staticmethod
    def get_user_permissions(
        user_id: str, session: Optional[Session] = None
    ) -> Dict[str, bool]:
        """
        Get all permissions for a user.

        Args:
            user_id: User ID
            session: Database session (optional)

        Returns:
            Dictionary of permissions
        """
        # DEV MODE: the same built-in Admin the permission check grants.
        if _is_dev_mode():
            from core.auth.permissions import CATALOG

            return dict(CATALOG["builtins"]["role-admin"])

        with unit_of_work(session) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                return {}

            role = session.query(Role).filter(Role.role_id == user.role_id).first()
            if not role:
                return {}

            return role.permissions

    @staticmethod
    @default_on_error(None)
    def create_user(
        username: str,
        email: str,
        password: str,
        full_name: str,
        role_id: str,
        session: Optional[Session] = None,
    ) -> Optional[User]:
        """
        Create a new user.

        Args:
            username: Username
            email: Email address
            password: Plain text password
            full_name: Full name
            role_id: Role ID
            session: Database session (optional)

        Returns:
            Created User object or None
        """
        with unit_of_work(session) as session:
            # Either string is a sign-in name, so each has to be free as a
            # username and as an email, not only in its own column.
            claim_sign_in_names(session, username, email)
            if identifier_in_use(session, username) or identifier_in_use(
                session, email
            ):
                logger.warning(f"User already exists: {username} or {email}")
                return None

            # Create user
            user = User(
                user_id=f"user-{uuid.uuid4().hex[:12]}",
                username=username,
                email=email,
                password_hash=AuthService.hash_password(password),
                full_name=full_name,
                role_id=role_id,
                is_active=True,
                is_verified=False,
                mfa_enabled=False,
                login_count=0,
            )

            principals.add_for_user(session, user)
            session.add(user)
            # Flush so the read-back sees server defaults; the unit of work
            # commits.
            session.flush()
            session.refresh(user)

            logger.info(f"User created: {username}")
            return user
