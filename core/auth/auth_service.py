"""
Authentication Service - User authentication and authorization.

Handles password hashing, JWT generation/validation, MFA, and session management.
"""

import base64
import hashlib
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import bcrypt
import jwt
import pyotp
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from core.config import get_settings
from core.exceptions import default_on_error
from core.secrets import get_secret
from core.storage.models import Role, User
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

logger = logging.getLogger(__name__)

# Fernet tokens are urlsafe base64 of a version byte + timestamp, so they start
# with "gAAAAA". Lowercase "g" is not base32, so legacy plaintext never matches.
_FERNET_TOKEN_PREFIX = "gAAAAA"
_UNDECRYPTABLE_LOG_INTERVAL = 60.0
_last_undecryptable_log = float("-inf")


class MFASecretDecryptionError(Exception):
    """A stored MFA secret is encrypted but not under the current JWT_SECRET_KEY."""


def _is_dev_mode() -> bool:
    return get_settings().dev_mode


def _load_jwt_secret() -> str:
    # Resolved at import time. DEV_MODE falls back to a deterministic dev secret
    # so tokens survive restarts; production fails closed when unset.
    value = get_secret("JWT_SECRET_KEY")

    if value:
        return value

    if _is_dev_mode():
        logger.warning(
            "JWT_SECRET_KEY not set; using deterministic DEV_MODE fallback. "
            "Tokens issued in dev are not portable to production."
        )
        return "dev-mode-insecure-jwt-secret-do-not-use-in-production"

    raise RuntimeError(
        "JWT_SECRET_KEY is required when DEV_MODE=false. "
        "Set it in the environment or .env before starting the backend."
    )


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

# JWT Configuration
JWT_SECRET_KEY = _load_jwt_secret()
JWT_ALGORITHM = "HS256"
JWT_ACCESS_EXPIRATION_MINUTES = get_settings().jwt_access_expiration_minutes
JWT_REFRESH_EXPIRATION_DAYS = get_settings().jwt_refresh_expiration_days

# Account lockout configuration
LOCKOUT_THRESHOLD = get_settings().auth_lockout_threshold
LOCKOUT_DURATION_MINUTES = get_settings().auth_lockout_duration_minutes

# Number of prior password hashes to remember and reject on reuse
PASSWORD_HISTORY_LIMIT = get_settings().auth_password_history_limit


def _is_service_account(user: User) -> bool:
    """Only an explicit True exempts from lockout; anything else fails closed.

    A user object without the field is not one. MFA lockout tests, and any
    caller that has not loaded the column, must still lock. An attribute
    read, not a string: the writer scan allows exactly one assignment.
    """
    try:
        return user.service_account is True
    except AttributeError:
        return False


def _account_is_locked(user) -> bool:
    until = getattr(user, "locked_until", None)
    return until is not None and until > utcnow()


class AccountLockedError(Exception):
    """Raised when authentication is refused because the account is locked."""

    def __init__(self, locked_until: datetime):
        self.locked_until = locked_until
        super().__init__(f"Account locked until {locked_until.isoformat()}")


def password_matches_any(plaintext: str, hashes) -> bool:
    """Return True if `plaintext` matches any bcrypt hash in the iterable."""
    if not hashes:
        return False
    encoded = plaintext.encode("utf-8")
    for h in hashes:
        if not h:
            continue
        try:
            if bcrypt.checkpw(encoded, h.encode("utf-8")):
                return True
        except Exception as exc:
            logger.warning("Password history compare failed for one entry: %s", exc)
    return False


class AuthService:
    """Service for user authentication and authorization."""

    @staticmethod
    def hash_password(password: str) -> str:
        """
        Hash a password using bcrypt.

        Args:
            password: Plain text password

        Returns:
            Hashed password
        """
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
        return hashed.decode("utf-8")

    @staticmethod
    @default_on_error(False)
    def verify_password(password: str, password_hash: str) -> bool:
        """
        Verify a password against its hash.

        Args:
            password: Plain text password
            password_hash: Hashed password

        Returns:
            True if password matches
        """
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))

    @staticmethod
    def _session_fingerprint(user_agent: Optional[str]) -> str:
        """Short SHA-256 fingerprint binding a token to its user-agent.

        Deliberately excludes the client IP: NAT, VPNs and mobile roaming
        change a client's IP within a token's lifetime (forcing spurious
        re-logins), and behind a reverse proxy every client presents the
        proxy's IP, so it adds no signal. The user-agent is a stable,
        defense-in-depth check on top of the JWT signature.
        """
        return hashlib.sha256((user_agent or "").encode()).hexdigest()[:16]

    @staticmethod
    def generate_jwt_token(
        user: User,
        token_type: str = "access",
        user_agent: Optional[str] = None,
    ) -> str:
        """Generate a JWT token for a user with optional session binding."""
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
            "exp": now + expiration,
            "iat": now,
        }

        # Bind token to session context when available
        if user_agent:
            payload["sfp"] = AuthService._session_fingerprint(user_agent)

        token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
        return token

    @staticmethod
    def verify_session_fingerprint(
        payload: Dict[str, Any],
        user_agent: Optional[str] = None,
    ) -> bool:
        """Check that a token's session fingerprint matches the request context.

        Returns True if no fingerprint is bound (backwards compat) or if it matches.
        """
        sfp = payload.get("sfp")
        if not sfp:
            return True
        expected = AuthService._session_fingerprint(user_agent)
        return secrets.compare_digest(sfp, expected)

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
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
            return payload
        except jwt.ExpiredSignatureError:
            logger.warning("JWT token expired")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            return None

    @staticmethod
    def authenticate_user(
        username_or_email: str, password: str, session: Optional[Session] = None
    ) -> Optional[User]:
        # The unit of work must see any failure, so it sits inside the try —
        # the handlers below swallow the exception and would otherwise let a
        # failed attempt commit.
        try:
            with unit_of_work(session) as session:
                # Try to find user by username or email
                user = (
                    session.query(User)
                    .filter(
                        (User.username == username_or_email)
                        | (User.email == username_or_email)
                    )
                    .first()
                )

                if not user:
                    logger.warning("Login attempt for unknown identifier")
                    return None

                if not user.is_active:
                    logger.warning(
                        "Login attempt for inactive account: %s", user.username
                    )
                    return None

                # Reject while locked. Lockout is authoritative even over a
                # correct password — otherwise an attacker who eventually
                # guesses right would bypass the wait.
                now = utcnow()
                if _account_is_locked(user) and not _is_service_account(user):
                    logger.warning(
                        "Login rejected, account locked: %s until %s",
                        user.username,
                        user.locked_until.isoformat(),
                    )
                    raise AccountLockedError(user.locked_until)

                # Verify password
                if not AuthService.verify_password(password, user.password_hash):
                    AuthService._record_failed_attempt(user.user_id, now)
                    logger.warning("Invalid password for user: %s", user.username)
                    return None

                # Success — reset lockout state and update session tracking
                user.failed_login_count = 0
                user.locked_until = None
                user.last_login = now
                user.login_count += 1

                logger.info(f"User authenticated successfully: {username_or_email}")
                return user

        except AccountLockedError:
            raise
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return None

    @staticmethod
    def _record_failed_attempt(user_id: str, now: datetime) -> None:
        """Persist one failed login attempt in its own transaction.

        Deliberately does not join the caller's transaction: the login endpoint
        answers a failed attempt with 401, which rolls that transaction back. If
        this bookkeeping went with it the counter would never survive, the
        threshold would never trip, and the lockout would silently do nothing.
        """
        with unit_of_work() as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                return
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if _is_service_account(user):
                # Never locked (D2-17): a lock would let anyone who learns the
                # name blind Medic's gateway. Safe only because the password is
                # long, random and held by the gateway alone. Still counted, and
                # logged once at the threshold so brute force stays visible.
                if user.failed_login_count == LOCKOUT_THRESHOLD:
                    logger.warning(
                        "Service account %s reached %d failed logins; "
                        "service accounts are not locked out",
                        user.username,
                        user.failed_login_count,
                    )
                return
            if user.failed_login_count >= LOCKOUT_THRESHOLD:
                user.locked_until = now + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
                logger.warning(
                    "Account locked after %d failed attempts: %s",
                    user.failed_login_count,
                    user.username,
                )

    @staticmethod
    def _lock_savepoint(session: Session):
        """Savepoint around a row lock, or None for a session that is not real.

        A wrong MFA code is counted in its own transaction, the same way a
        wrong password is. That transaction cannot take the row while this
        one holds FOR UPDATE, and a savepoint rollback is what releases the
        lock. Unit tests pass a fake session; there is no second connection
        to commit on, and the caller is not about to roll a real 401 back.
        """
        if not isinstance(session, Session):
            return None
        return session.begin_nested()

    @staticmethod
    def _close_savepoint(nested) -> None:
        if nested is not None and getattr(nested, "is_active", False):
            nested.commit()

    @staticmethod
    def _count_mfa_failure(user_id: str, username: str) -> None:
        """Record a wrong code after the row lock has been released."""
        AuthService._record_failed_attempt(user_id, utcnow())
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
        failed: Optional[tuple[str, str]] = None
        result: Optional[List[str]] = None
        with unit_of_work(session) as session:
            nested = AuthService._lock_savepoint(session)
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
                        if nested is not None:
                            failed = (user.user_id, user.username)
                    elif user.mfa_enabled:
                        # Already enabled — codes were issued at enable time,
                        # don't silently reissue. Use regenerate to rotate.
                        result = []
                    else:
                        user.mfa_enabled = True
                        user.failed_login_count = 0
                        user.locked_until = None
                        codes, user.mfa_recovery_codes = (
                            AuthService._generate_recovery_codes()
                        )
                        logger.info(f"MFA enabled for user: {user.username}")
                        result = codes
            finally:
                if failed is not None and nested is not None:
                    nested.rollback()
                else:
                    AuthService._close_savepoint(nested)
        if failed:
            AuthService._count_mfa_failure(failed[0], failed[1])
        return result

    @staticmethod
    @default_on_error(None)
    def get_mfa_recovery_codes(
        user_id: str, session: Optional[Session] = None
    ) -> Optional[List[str]]:
        """Generate fresh recovery codes for a user (re-setup).

        Returns plaintext codes. Caller must display them once; they cannot
        be retrieved again.
        """
        with unit_of_work(session) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user or not user.mfa_secret:
                return None

            codes, user.mfa_recovery_codes = AuthService._generate_recovery_codes()
            return codes

    @staticmethod
    @default_on_error(False)
    def verify_mfa_code(
        user_id: str, code: str, session: Optional[Session] = None
    ) -> bool:
        """Verify a TOTP code or a one-time recovery code (login path).

        Enabling MFA is handled by enable_mfa(); this method assumes MFA is
        already enabled and only validates the supplied code.
        """
        failed: Optional[tuple[str, str]] = None
        accepted = False
        with unit_of_work(session) as session:
            nested = AuthService._lock_savepoint(session)
            try:
                # Locked so two requests carrying the same code are serialised:
                # the second sees the consumed recovery code / stored TOTP step.
                user = AuthService._lock_user(session, user_id)
                if not user or not user.mfa_secret or _account_is_locked(user):
                    return False

                # Try TOTP first. An undecryptable secret must not block recovery
                # codes: they are bcrypt hashes and do not depend on the key.
                try:
                    totp_ok = AuthService._accept_totp(user, code)
                except MFASecretDecryptionError:
                    totp_ok = False  # already logged at ERROR with the cause
                if totp_ok:
                    accepted = True
                else:
                    recovery_codes = list(user.mfa_recovery_codes or [])
                    code_bytes = code.strip().upper().encode()
                    for i, hashed in enumerate(recovery_codes):
                        try:
                            if bcrypt.checkpw(code_bytes, hashed.encode()):
                                recovery_codes.pop(i)
                                user.mfa_recovery_codes = recovery_codes
                                logger.info(
                                    "Recovery code used for user: %s (%d remaining)",
                                    user.username,
                                    len(recovery_codes),
                                )
                                accepted = True
                                break
                        except Exception:
                            continue
                    if not accepted and nested is not None:
                        failed = (user.user_id, user.username)
            finally:
                if failed is not None and nested is not None:
                    nested.rollback()
                else:
                    AuthService._close_savepoint(nested)
        if failed:
            # The savepoint is already closed; count on a fresh transaction.
            AuthService._count_mfa_failure(failed[0], failed[1])
        return accepted

    @staticmethod
    @default_on_error(False)
    def disable_mfa(user_id: str, code: str, session: Optional[Session] = None) -> bool:
        """Turn MFA off after proving possession of a TOTP or recovery code.

        Clears the secret and any remaining recovery codes. Returns False
        (leaving MFA untouched) if MFA is not enabled or the code is wrong.
        """
        with unit_of_work(session) as session:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user or not user.mfa_enabled:
                return False
            if not AuthService.verify_mfa_code(user_id, code, session):
                return False

            user.mfa_enabled = False
            user.mfa_secret = None
            user.mfa_recovery_codes = []
            user.failed_login_count = 0
            user.locked_until = None
            logger.info(f"MFA disabled for user: {user.username}")
            return True

    @staticmethod
    def _generate_recovery_codes() -> tuple[list[str], list[str]]:
        """Return (plaintext, bcrypt-hashed) lists of 10 one-time recovery codes."""
        codes = [secrets.token_hex(4).upper() for _ in range(10)]
        hashed = [bcrypt.hashpw(c.encode(), bcrypt.gensalt()).decode() for c in codes]
        return codes, hashed

    @staticmethod
    def _fernet() -> Fernet:
        """Fernet cipher keyed off JWT_SECRET_KEY (SHA-256 -> base64 32-byte key)."""
        key = base64.urlsafe_b64encode(hashlib.sha256(JWT_SECRET_KEY.encode()).digest())
        return Fernet(key)

    @staticmethod
    def _encrypt_mfa_secret(secret: str) -> str:
        """Encrypt an MFA secret at rest with Fernet (JWT-key-derived)."""
        return AuthService._fernet().encrypt(secret.encode()).decode()

    @staticmethod
    def _decrypt_mfa_secret(encrypted: str) -> str:
        """Decrypt an MFA secret; fall back to plaintext for pre-migration rows.

        Raises:
            MFASecretDecryptionError: the value is a Fernet token that the
                current JWT_SECRET_KEY cannot decrypt.
        """
        try:
            return AuthService._fernet().decrypt(encrypted.encode()).decode()
        except Exception as exc:
            if not encrypted.startswith(_FERNET_TOKEN_PREFIX):
                return encrypted  # legacy plaintext (pre-migration rows)
            AuthService._log_undecryptable_secret()
            raise MFASecretDecryptionError(
                "MFA secret cannot be decrypted with the current JWT_SECRET_KEY"
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
            "MFA secret cannot be decrypted with the current JWT_SECRET_KEY; "
            "the key has changed since MFA was set up. TOTP codes are rejected "
            "until the original key is restored; recovery codes still work."
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
        # DEV MODE: Grant all permissions
        if _is_dev_mode():
            return True

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
        # DEV MODE: Return all permissions
        if _is_dev_mode():
            return {
                "findings.read": True,
                "findings.write": True,
                "findings.delete": True,
                "cases.read": True,
                "cases.write": True,
                "cases.delete": True,
                "cases.assign": True,
                "integrations.read": True,
                "integrations.write": True,
                "users.read": True,
                "users.write": True,
                "users.delete": True,
                "settings.read": True,
                "settings.write": True,
                "ai_chat.use": True,
                "ai_decisions.approve": True,
                "medic.read": True,
                "medic.admin": True,
                "audit.read": True,
                "audit.forward": True,
                "audit.manage": True,
            }

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
            import uuid

            # Check if username or email already exists
            existing = (
                session.query(User)
                .filter((User.username == username) | (User.email == email))
                .first()
            )

            if existing:
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

            session.add(user)
            # Flush so the read-back sees server defaults; the unit of work
            # commits.
            session.flush()
            session.refresh(user)

            logger.info(f"User created: {username}")
            return user
