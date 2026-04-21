"""
Authentication Middleware - JWT validation and RBAC enforcement.

Provides middleware for FastAPI to validate JWT tokens and check permissions.
Supports DEV_MODE for bypassing authentication during development.
"""

import logging
import os
from typing import Optional, Callable
from functools import wraps
from fastapi import HTTPException, Header, Depends, Request, status
from sqlalchemy.orm import Session

from backend.services.auth_cookies import ACCESS_COOKIE_NAME
from backend.services.auth_service import AuthService
from backend.services.token_blacklist import is_token_revoked
from database.models import User
from database.connection import get_db_session

logger = logging.getLogger(__name__)

# Dev mode flag - ONLY for development, never in production!
DEV_MODE = os.getenv("DEV_MODE", "false").lower() in ("true", "1", "yes")

if DEV_MODE:
    logger.warning("⚠️  DEV_MODE is ENABLED - Authentication is BYPASSED!")
    logger.warning("⚠️  This should NEVER be enabled in production!")

# Mock dev user for dev mode
_dev_user = None


def _get_dev_user(session: Session) -> User:
    """
    Get or create a mock dev user for development mode.
    
    Args:
        session: Database session
    
    Returns:
        Mock dev user with admin permissions
    """
    global _dev_user
    
    if _dev_user is None:
        # Try to get existing admin user
        _dev_user = session.query(User).filter(User.username == "admin").first()
        
        # If no admin, create a mock user object (won't be persisted)
        if _dev_user is None:
            from database.models import Role
            import uuid
            
            # Try to get admin role
            admin_role = session.query(Role).filter(Role.name == "admin").first()
            
            # Create mock user
            _dev_user = User(
                user_id=str(uuid.uuid4()),
                username="dev-user",
                email="dev@localhost",
                password_hash="",  # Not used in dev mode
                role_id=admin_role.role_id if admin_role else str(uuid.uuid4()),
                is_active=True,
                mfa_enabled=False
            )
            logger.info("Created mock dev user (not persisted to DB)")
    
    return _dev_user


async def get_current_user(
    request: Request,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_db_session)
) -> User:
    """
    Resolve the authenticated user from either the access_token HttpOnly
    cookie (browser flow) or an Authorization: Bearer header (API clients).

    Cookie is preferred when both are present.

    In DEV_MODE, authentication is bypassed and a mock admin user is returned.

    Args:
        request: FastAPI request (used to read the access_token cookie).
        authorization: Authorization header (Bearer token fallback).
        session: Database session.

    Returns:
        Current User object.

    Raises:
        HTTPException: If no token is present, validation fails, or the user
            is not found (production only).
    """
    # DEV MODE: Bypass authentication and return mock user
    if DEV_MODE:
        logger.debug("DEV_MODE: Bypassing authentication")
        return _get_dev_user(session)

    token: Optional[str] = request.cookies.get(ACCESS_COOKIE_NAME)

    if not token and authorization:
        # Bearer fallback for CLI / scripts / integrations that haven't
        # migrated to the cookie flow.
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authorization header format",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = parts[1]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Verify JWT token
    payload = AuthService.verify_jwt_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Reject revoked tokens (logout / password change / role change).
    # Fail-open if Redis is down — see token_blacklist.is_token_revoked.
    if await is_token_revoked(payload):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Get user from database
    user = session.query(User).filter(User.user_id == payload["user_id"]).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )
    
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """
    Dependency to get current active user.
    
    Args:
        current_user: Current user from get_current_user
    
    Returns:
        Current active User object
    """
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive"
        )
    return current_user


def require_permission(permission: str):
    """
    Decorator to require a specific permission for an endpoint.
    
    Usage:
        @router.get("/cases")
        @require_permission("cases.read")
        async def get_cases(current_user: User = Depends(get_current_user)):
            ...
    
    Args:
        permission: Permission string (e.g., "cases.write")
    
    Returns:
        Decorated function
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, current_user: User = Depends(get_current_user), **kwargs):
            # Check if user has permission
            has_permission = AuthService.check_permission(
                current_user.user_id,
                permission
            )
            
            if not has_permission:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: {permission} required"
                )
            
            return await func(*args, current_user=current_user, **kwargs)
        
        return wrapper
    return decorator


def require_any_permission(*permissions: str):
    """
    Decorator to require any of the specified permissions.
    
    Args:
        permissions: Permission strings
    
    Returns:
        Decorated function
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, current_user: User = Depends(get_current_user), **kwargs):
            # Check if user has any of the permissions
            user_permissions = AuthService.get_user_permissions(current_user.user_id)
            
            has_any = any(user_permissions.get(perm, False) for perm in permissions)
            
            if not has_any:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: One of {permissions} required"
                )
            
            return await func(*args, current_user=current_user, **kwargs)
        
        return wrapper
    return decorator


def require_all_permissions(*permissions: str):
    """
    Decorator to require all of the specified permissions.
    
    Args:
        permissions: Permission strings
    
    Returns:
        Decorated function
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, current_user: User = Depends(get_current_user), **kwargs):
            # Check if user has all permissions
            user_permissions = AuthService.get_user_permissions(current_user.user_id)
            
            has_all = all(user_permissions.get(perm, False) for perm in permissions)
            
            if not has_all:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: All of {permissions} required"
                )
            
            return await func(*args, current_user=current_user, **kwargs)
        
        return wrapper
    return decorator


def require_role(role_name: str):
    """
    Decorator to require a specific role.
    
    Args:
        role_name: Role name (e.g., "Admin")
    
    Returns:
        Decorated function
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, current_user: User = Depends(get_current_user), session: Session = Depends(get_db_session), **kwargs):
            from database.models import Role
            
            # Get user's role
            role = session.query(Role).filter(Role.role_id == current_user.role_id).first()
            
            if not role or role.name != role_name:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Role '{role_name}' required"
                )
            
            return await func(*args, current_user=current_user, session=session, **kwargs)
        
        return wrapper
    return decorator


# Optional authentication (doesn't raise error if no token)
async def get_optional_user(
    request: Request,
    authorization: Optional[str] = Header(None),
    session: Session = Depends(get_db_session)
) -> Optional[User]:
    """
    Dependency to optionally get the current user.

    Returns None if no valid token is provided (either via cookie or
    Authorization header).

    Args:
        request: FastAPI request (used to read the access_token cookie).
        authorization: Authorization header (Bearer token fallback).
        session: Database session

    Returns:
        User object or None
    """
    has_cookie = ACCESS_COOKIE_NAME in request.cookies
    if not has_cookie and not authorization:
        return None

    try:
        return await get_current_user(request, authorization, session)
    except HTTPException:
        return None

