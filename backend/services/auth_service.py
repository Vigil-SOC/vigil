"""
Authentication Service - User authentication and authorization.

Handles password hashing, JWT generation/validation, MFA, and session management.
"""

import base64
import hashlib
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import bcrypt
import jwt
import pyotp
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from database.models import User, Role
from database.connection import get_db_session

logger = logging.getLogger(__name__)


def _is_dev_mode() -> bool:
    return os.getenv("DEV_MODE", "false").lower() in ("true", "1", "yes")


def _load_jwt_secret() -> str:
    """
    Load the JWT signing secret at import time.

    Priority: env var / secrets backend. In DEV_MODE, fall back to a
    deterministic dev secret so tokens survive restarts locally. In
    production (DEV_MODE=false), fail-closed at startup if unset.
    """
    try:
        from backend.secrets_manager import get_secret

        value = get_secret("JWT_SECRET_KEY")
    except Exception:
        value = os.environ.get("JWT_SECRET_KEY")

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


# JWT Configuration
JWT_SECRET_KEY = _load_jwt_secret()
JWT_ALGORITHM = "HS256"
JWT_ACCESS_EXPIRATION_MINUTES = int(os.getenv("JWT_ACCESS_EXPIRATION_MINUTES", "30"))
JWT_REFRESH_EXPIRATION_DAYS = int(os.getenv("JWT_REFRESH_EXPIRATION_DAYS", "7"))

# Account lockout configuration
LOCKOUT_THRESHOLD = int(os.getenv("AUTH_LOCKOUT_THRESHOLD", "5"))
LOCKOUT_DURATION_MINUTES = int(os.getenv("AUTH_LOCKOUT_DURATION_MINUTES", "15"))

# Number of prior password hashes to remember and reject on reuse
PASSWORD_HISTORY_LIMIT = int(os.getenv("AUTH_PASSWORD_HISTORY_LIMIT", "5"))


class AccountLockedError(Exception):
    """Raised when authentication is refused because the account is locked."""

    def __init__(self, locked_until: datetime):
        self.locked_until = locked_until
        super().__init__(f"Account locked until {locked_until.isoformat()}")


class PasswordReuseError(Exception):
    """Raised when a user attempts to reuse a recent password."""


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
    def verify_password(password: str, password_hash: str) -> bool:
        """
        Verify a password against its hash.

        Args:
            password: Plain text password
            password_hash: Hashed password

        Returns:
            True if password matches
        """
        try:
            return bcrypt.checkpw(
                password.encode("utf-8"), password_hash.encode("utf-8")
            )
        except Exception as e:
            logger.error(f"Password verification error: {e}")
            return False

    @staticmethod
    def _session_fingerprint(
        request_ip: Optional[str], user_agent: Optional[str]
    ) -> str:
        """Short SHA-256 fingerprint binding a token to its IP + user-agent."""
        fp = f"{request_ip or ''}|{user_agent or ''}"
        return hashlib.sha256(fp.encode()).hexdigest()[:16]

    @staticmethod
    def generate_jwt_token(
        user: User,
        token_type: str = "access",
        request_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> str:
        """Generate a JWT token for a user with optional session binding."""
        expiration = (
            timedelta(minutes=JWT_ACCESS_EXPIRATION_MINUTES)
            if token_type == "access"
            else timedelta(days=JWT_REFRESH_EXPIRATION_DAYS)
        )

        now = datetime.utcnow()
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
        if request_ip or user_agent:
            payload["sfp"] = AuthService._session_fingerprint(request_ip, user_agent)

        token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
        return token

    @staticmethod
    def verify_session_fingerprint(
        payload: Dict[str, Any],
        request_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> bool:
        """Check that a token's session fingerprint matches the request context.

        Returns True if no fingerprint is bound (backwards compat) or if it matches.
        """
        sfp = payload.get("sfp")
        if not sfp:
            return True
        expected = AuthService._session_fingerprint(request_ip, user_agent)
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
        """
        Authenticate a user with username/email and password.

        Args:
            username_or_email: Username or email
            password: Plain text password
            session: Database session (optional)

        Returns:
            User object if authentication successful, None otherwise
        """
        should_close_session = session is None
        session = session or get_db_session()

        try:
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
                logger.warning("Login attempt for inactive account: %s", user.username)
                return None

            # Reject while locked. Lockout is authoritative even over a correct
            # password — otherwise an attacker who eventually guesses right
            # would bypass the wait.
            now = datetime.utcnow()
            if user.locked_until and user.locked_until > now:
                logger.warning(
                    "Login rejected, account locked: %s until %s",
                    user.username,
                    user.locked_until.isoformat(),
                )
                raise AccountLockedError(user.locked_until)

            # Verify password
            if not AuthService.verify_password(password, user.password_hash):
                user.failed_login_count = (user.failed_login_count or 0) + 1
                if user.failed_login_count >= LOCKOUT_THRESHOLD:
                    user.locked_until = now + timedelta(
                        minutes=LOCKOUT_DURATION_MINUTES
                    )
                    logger.warning(
                        "Account locked after %d failed attempts: %s",
                        user.failed_login_count,
                        user.username,
                    )
                session.commit()
                logger.warning("Invalid password for user: %s", user.username)
                return None

            # Success — reset lockout state and update session tracking
            user.failed_login_count = 0
            user.locked_until = None
            user.last_login = now
            user.login_count += 1
            session.commit()

            logger.info(f"User authenticated successfully: {username_or_email}")
            return user

        except AccountLockedError:
            raise
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            session.rollback()
            return None

        finally:
            if should_close_session:
                session.close()

    @staticmethod
    def setup_mfa(user_id: str, session: Optional[Session] = None) -> Optional[str]:
        """Setup MFA. Returns TOTP secret and generates recovery codes."""
        should_close_session = session is None
        session = session or get_db_session()

        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                return None

            secret = pyotp.random_base32()
            user.mfa_secret = AuthService._encrypt_mfa_secret(secret)
            user.mfa_enabled = False
            # Generate 10 recovery codes, store bcrypt-hashed (plaintext
            # is surfaced via get_mfa_recovery_codes, not here)
            _, user.mfa_recovery_codes = AuthService._generate_recovery_codes()
            session.commit()

            logger.info(f"MFA setup initiated for user: {user.username}")
            # Return plaintext secret; recovery codes returned separately
            return secret

        except Exception as e:
            logger.error(f"MFA setup error: {e}")
            session.rollback()
            return None

        finally:
            if should_close_session:
                session.close()

    @staticmethod
    def get_mfa_recovery_codes(
        user_id: str, session: Optional[Session] = None
    ) -> Optional[List[str]]:
        """Generate fresh recovery codes for a user (re-setup).

        Returns plaintext codes. Caller must display them once; they cannot
        be retrieved again.
        """
        should_close_session = session is None
        session = session or get_db_session()

        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user or not user.mfa_secret:
                return None

            codes, user.mfa_recovery_codes = AuthService._generate_recovery_codes()
            session.commit()
            return codes

        except Exception as e:
            logger.error(f"Recovery code generation error: {e}")
            session.rollback()
            return None

        finally:
            if should_close_session:
                session.close()

    @staticmethod
    def verify_mfa_code(
        user_id: str, code: str, session: Optional[Session] = None
    ) -> bool:
        """Verify a TOTP code or a one-time recovery code."""
        should_close_session = session is None
        session = session or get_db_session()

        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user or not user.mfa_secret:
                return False

            # Try TOTP first
            decrypted_secret = AuthService._decrypt_mfa_secret(user.mfa_secret)
            totp = pyotp.TOTP(decrypted_secret)
            if totp.verify(code, valid_window=1):
                if not user.mfa_enabled:
                    user.mfa_enabled = True
                    session.commit()
                    logger.info(f"MFA enabled for user: {user.username}")
                return True

            # Try recovery codes (one-time use)
            recovery_codes = list(user.mfa_recovery_codes or [])
            code_bytes = code.strip().upper().encode()
            for i, hashed in enumerate(recovery_codes):
                try:
                    if bcrypt.checkpw(code_bytes, hashed.encode()):
                        recovery_codes.pop(i)
                        user.mfa_recovery_codes = recovery_codes
                        session.commit()
                        logger.info(
                            "Recovery code used for user: %s (%d remaining)",
                            user.username,
                            len(recovery_codes),
                        )
                        return True
                except Exception:
                    continue

            return False

        except Exception as e:
            logger.error(f"MFA verification error: {e}")
            return False

        finally:
            if should_close_session:
                session.close()

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
        """Decrypt an MFA secret; fall back to plaintext for pre-migration rows."""
        try:
            return AuthService._fernet().decrypt(encrypted.encode()).decode()
        except Exception:
            # Fallback: secret may be stored unencrypted (pre-migration rows)
            return encrypted

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
        should_close_session = session is None
        session = session or get_db_session()

        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user or not user.mfa_secret:
                return None

            totp = pyotp.TOTP(user.mfa_secret)
            uri = totp.provisioning_uri(name=user.email, issuer_name="Vigil SOC")
            return uri

        finally:
            if should_close_session:
                session.close()

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
        import os

        DEV_MODE = os.getenv("DEV_MODE", "false").lower() in ("true", "1", "yes")
        if DEV_MODE:
            return True

        should_close_session = session is None
        session = session or get_db_session()

        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user or not user.is_active:
                return False

            role = session.query(Role).filter(Role.role_id == user.role_id).first()
            if not role:
                return False

            # Check permission in role's permissions JSONB
            return role.permissions.get(permission, False)

        finally:
            if should_close_session:
                session.close()

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
        import os

        DEV_MODE = os.getenv("DEV_MODE", "false").lower() in ("true", "1", "yes")
        if DEV_MODE:
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
            }

        should_close_session = session is None
        session = session or get_db_session()

        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                return {}

            role = session.query(Role).filter(Role.role_id == user.role_id).first()
            if not role:
                return {}

            return role.permissions

        finally:
            if should_close_session:
                session.close()

    @staticmethod
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
        should_close_session = session is None
        session = session or get_db_session()

        try:
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
            session.commit()
            session.refresh(user)

            logger.info(f"User created: {username}")
            return user

        except Exception as e:
            logger.error(f"User creation error: {e}")
            session.rollback()
            return None

        finally:
            if should_close_session:
                session.close()
