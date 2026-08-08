"""Serialization schemas for the user and role models."""

from typing import Any, Optional

from core.storage.schemas.base import OptDateTime, ORMSchema


class UserSchema(ORMSchema):
    """User."""

    user_id: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    full_name: Optional[str] = None
    role_id: Optional[str] = None
    is_active: Optional[bool] = None
    is_verified: Optional[bool] = None
    mfa_enabled: Optional[bool] = None
    last_login: OptDateTime = None
    login_count: Optional[int] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class RoleSchema(ORMSchema):
    """Role."""

    role_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[Any] = None
    is_system_role: Optional[bool] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None
