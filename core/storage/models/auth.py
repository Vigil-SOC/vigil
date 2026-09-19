"""User and Role ORM models."""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base
from core.time import utcnow


class User(Base):
    """
    User Model - System users with authentication and authorization.

    Stores user credentials, profile information, and role assignments.
    """

    __tablename__ = "users"

    # Primary key
    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Authentication
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Profile
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Role and permissions
    role_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("roles.role_id"), nullable=False
    )

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # MFA
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mfa_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    mfa_recovery_codes: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list
    )

    # Session tracking
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Failed-login tracking and account lockout
    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Password history — list of prior bcrypt hashes, newest first. Used
    # to reject reuse of the last N passwords. Capped in application code.
    password_history: Mapped[List[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (
        Index("idx_user_username", "username"),
        Index("idx_user_email", "email"),
        Index("idx_user_role_id", "role_id"),
        Index("idx_user_is_active", "is_active"),
    )


class Role(Base):
    """
    Role Model - Defines user roles and their permissions.

    RBAC (Role-Based Access Control) system for authorization.
    """

    __tablename__ = "roles"

    # Primary key
    role_id: Mapped[str] = mapped_column(String(50), primary_key=True)

    # Role details
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Permissions (JSONB for flexibility)
    permissions: Mapped[dict] = mapped_column(JSONB, nullable=False, default={})

    # System role flag (cannot be deleted/modified)
    is_system_role: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default="now()",
    )

    # Indexes
    __table_args__ = (Index("idx_role_name", "name"),)


class McpCredential(Base):
    """A credential a program holds, to reach Vigil's MCP surface.

    Tied to a user and carrying no permissions of its own: what the holder may
    do is what that user may do. See ``infra/database/init/34_mcp_credentials.sql``
    for why it is a table of its own rather than columns on ``users``.
    """

    __tablename__ = "mcp_credentials"

    credential_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(50), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )

    # SHA-256 of a token this never stores. The token is 256 bits from a
    # CSPRNG, so there is nothing to guess; bcrypt's work factor is for a
    # secret a person chose, and would be paid on every call a program makes.
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (Index("idx_mcp_credentials_user", "user_id"),)

    def is_usable(self, now: Optional[datetime] = None) -> bool:
        """Whether this credential may authenticate a call right now."""
        now = now or utcnow()
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and self.expires_at <= now:
            return False
        return True

    def to_dict(self) -> dict:
        """What an operator may see. Never the token; it no longer exists."""
        return {
            "credential_id": self.credential_id,
            "user_id": self.user_id,
            "label": self.label,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_used_at": (
                self.last_used_at.isoformat() if self.last_used_at else None
            ),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }
