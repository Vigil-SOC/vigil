"""
Authentication Service - User authentication and authorization.

Handles password hashing, JWT generation/validation, MFA, and session management.
"""

import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import bcrypt
import jwt
import pyotp
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
JWT_EXPIRATION_HOURS = 24
JWT_REFRESH_EXPIRATION_DAYS = 30

# Account lockout configuration
LOCKOUT_THRESHOLD = int(os.getenv("AUTH_LOCKOUT_THRESHOLD", "5"))
LOCKOUT_DURATION_MINUTES = int(os.getenv("AUTH_LOCKOUT_DURATION_MINUTES", "15"))


class AccountLockedError(Exception):
    """Raised when authentication is refused because the account is locked."""

    def __init__(self, locked_until: datetime):
        self.locked_until = locked_until
        super().__init__(f"Account locked until {locked_until.isoformat()}")


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
        hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
        return hashed.decode('utf-8')
    
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
            return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
        except Exception as e:
            logger.error(f"Password verification error: {e}")
            return False
    
    @staticmethod
    def generate_jwt_token(user: User, token_type: str = "access") -> str:
        """
        Generate a JWT token for a user.
        
        Args:
            user: User object
            token_type: "access" or "refresh"
        
        Returns:
            JWT token string
        """
        expiration = timedelta(
            hours=JWT_EXPIRATION_HOURS if token_type == "access" else JWT_REFRESH_EXPIRATION_DAYS * 24
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
        
        token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
        return token
    
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
    def authenticate_user(username_or_email: str, password: str, session: Optional[Session] = None) -> Optional[User]:
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
        if session is None:
            session = get_db_session()
        
        try:
            # Try to find user by username or email
            user = session.query(User).filter(
                (User.username == username_or_email) | (User.email == username_or_email)
            ).first()

            if not user:
                logger.warning(f"User not found: {username_or_email}")
                return None

            if not user.is_active:
                logger.warning(f"User is inactive: {username_or_email}")
                return None

            # Reject while locked. Lockout is authoritative even over a correct
            # password — otherwise an attacker who eventually guesses right
            # would bypass the wait.
            now = datetime.utcnow()
            if user.locked_until and user.locked_until > now:
                logger.warning(
                    f"Login rejected, account locked: {username_or_email} "
                    f"until {user.locked_until.isoformat()}"
                )
                raise AccountLockedError(user.locked_until)

            # Verify password
            if not AuthService.verify_password(password, user.password_hash):
                user.failed_login_count = (user.failed_login_count or 0) + 1
                if user.failed_login_count >= LOCKOUT_THRESHOLD:
                    user.locked_until = now + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
                    logger.warning(
                        f"Account locked after {user.failed_login_count} failed "
                        f"attempts: {username_or_email}"
                    )
                session.commit()
                logger.warning(f"Invalid password for user: {username_or_email}")
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
        """
        Setup MFA for a user.
        
        Args:
            user_id: User ID
            session: Database session (optional)
        
        Returns:
            MFA secret (base32 encoded) or None
        """
        should_close_session = session is None
        if session is None:
            session = get_db_session()
        
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user:
                return None
            
            # Generate MFA secret
            secret = pyotp.random_base32()
            user.mfa_secret = secret
            user.mfa_enabled = False  # Will be enabled after verification
            session.commit()
            
            logger.info(f"MFA setup initiated for user: {user.username}")
            return secret
        
        except Exception as e:
            logger.error(f"MFA setup error: {e}")
            session.rollback()
            return None
        
        finally:
            if should_close_session:
                session.close()
    
    @staticmethod
    def verify_mfa_code(user_id: str, code: str, session: Optional[Session] = None) -> bool:
        """
        Verify an MFA code.
        
        Args:
            user_id: User ID
            code: 6-digit MFA code
            session: Database session (optional)
        
        Returns:
            True if code is valid
        """
        should_close_session = session is None
        if session is None:
            session = get_db_session()
        
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user or not user.mfa_secret:
                return False
            
            # Verify TOTP code
            totp = pyotp.TOTP(user.mfa_secret)
            is_valid = totp.verify(code, valid_window=1)  # Allow 1 time step window
            
            if is_valid and not user.mfa_enabled:
                # First successful verification - enable MFA
                user.mfa_enabled = True
                session.commit()
                logger.info(f"MFA enabled for user: {user.username}")
            
            return is_valid
        
        except Exception as e:
            logger.error(f"MFA verification error: {e}")
            return False
        
        finally:
            if should_close_session:
                session.close()
    
    @staticmethod
    def get_mfa_qr_uri(user_id: str, session: Optional[Session] = None) -> Optional[str]:
        """
        Get MFA QR code URI for a user.
        
        Args:
            user_id: User ID
            session: Database session (optional)
        
        Returns:
            QR code URI or None
        """
        should_close_session = session is None
        if session is None:
            session = get_db_session()
        
        try:
            user = session.query(User).filter(User.user_id == user_id).first()
            if not user or not user.mfa_secret:
                return None
            
            totp = pyotp.TOTP(user.mfa_secret)
            uri = totp.provisioning_uri(
                name=user.email,
                issuer_name="Vigil SOC"
            )
            return uri
        
        finally:
            if should_close_session:
                session.close()
    
    @staticmethod
    def check_permission(user_id: str, permission: str, session: Optional[Session] = None) -> bool:
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
        if session is None:
            session = get_db_session()
        
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
    def get_user_permissions(user_id: str, session: Optional[Session] = None) -> Dict[str, bool]:
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
                'findings.read': True,
                'findings.write': True,
                'findings.delete': True,
                'cases.read': True,
                'cases.write': True,
                'cases.delete': True,
                'cases.assign': True,
                'integrations.read': True,
                'integrations.write': True,
                'users.read': True,
                'users.write': True,
                'users.delete': True,
                'settings.read': True,
                'settings.write': True,
                'ai_chat.use': True,
                'ai_decisions.approve': True,
            }
        
        should_close_session = session is None
        if session is None:
            session = get_db_session()
        
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
        session: Optional[Session] = None
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
        if session is None:
            session = get_db_session()
        
        try:
            import uuid
            
            # Check if username or email already exists
            existing = session.query(User).filter(
                (User.username == username) | (User.email == email)
            ).first()
            
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
                login_count=0
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

