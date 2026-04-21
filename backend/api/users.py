"""
User Management API - Admin endpoints for managing users.

Handles user CRUD operations, role assignment, and user administration.
"""

import logging
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Depends, status, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from backend.services.auth_service import AuthService
from backend.middleware.auth import get_current_user
from database.models import User, Role
from database.connection import get_db_session

logger = logging.getLogger(__name__)

router = APIRouter()


# Request/Response Models
class CreateUserRequest(BaseModel):
    """Create user request."""
    username: str
    email: EmailStr
    password: str
    full_name: str
    role_id: str


class UpdateUserRequest(BaseModel):
    """Update user request."""
    full_name: Optional[str] = None
    email: Optional[EmailStr] = None
    role_id: Optional[str] = None
    is_active: Optional[bool] = None


class ChangeUserRoleRequest(BaseModel):
    """Change user role request."""
    role_id: str


@router.get("/")
async def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    role_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    search: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session)
):
    """
    List all users (requires users.read permission).
    
    Args:
        skip: Number of users to skip
        limit: Maximum number of users to return
        role_id: Filter by role ID
        is_active: Filter by active status
        search: Search in username, email, or full name
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        List of users
    """
    # Check permission
    if not AuthService.check_permission(current_user.user_id, "users.read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: users.read required"
        )
    
    try:
        query = session.query(User)
        
        # Apply filters
        if role_id:
            query = query.filter(User.role_id == role_id)
        
        if is_active is not None:
            query = query.filter(User.is_active == is_active)
        
        if search:
            search_pattern = f"%{search}%"
            query = query.filter(
                (User.username.ilike(search_pattern)) |
                (User.email.ilike(search_pattern)) |
                (User.full_name.ilike(search_pattern))
            )
        
        # Get total count
        total = query.count()
        
        # Apply pagination
        users = query.offset(skip).limit(limit).all()
        
        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "users": [user.to_dict() for user in users]
        }
    
    except Exception as e:
        logger.error(f"List users error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list users"
        )


@router.get("/{user_id}")
async def get_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session)
):
    """
    Get user by ID (requires users.read permission).
    
    Args:
        user_id: User ID
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        User information
    """
    # Check permission (or allow users to view their own profile)
    if user_id != current_user.user_id:
        if not AuthService.check_permission(current_user.user_id, "users.read"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Permission denied: users.read required"
            )
    
    user = session.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    user_dict = user.to_dict()
    
    # Add role information
    role = session.query(Role).filter(Role.role_id == user.role_id).first()
    if role:
        user_dict["role"] = role.to_dict()
    
    # Add permissions
    user_dict["permissions"] = AuthService.get_user_permissions(user_id)
    
    return user_dict


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_user(
    request: CreateUserRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session)
):
    """
    Create a new user (requires users.write permission).
    
    Args:
        request: User creation details
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        Created user information
    """
    # Check permission
    if not AuthService.check_permission(current_user.user_id, "users.write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: users.write required"
        )
    
    # Validate password
    if len(request.password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters"
        )
    
    # Verify role exists
    role = session.query(Role).filter(Role.role_id == request.role_id).first()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid role ID"
        )
    
    # Create user
    user = AuthService.create_user(
        username=request.username,
        email=request.email,
        password=request.password,
        full_name=request.full_name,
        role_id=request.role_id,
        session=session
    )
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already exists"
        )
    
    logger.info(f"User created by {current_user.username}: {user.username}")
    return user.to_dict()


@router.put("/{user_id}")
async def update_user(
    user_id: str,
    request: UpdateUserRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session)
):
    """
    Update user information (requires users.write permission).
    
    Args:
        user_id: User ID to update
        request: Update details
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        Updated user information
    """
    # Check permission
    if not AuthService.check_permission(current_user.user_id, "users.write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: users.write required"
        )
    
    # Get user
    user = session.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    try:
        # Update fields
        if request.full_name is not None:
            user.full_name = request.full_name
        
        if request.email is not None:
            # Check if email is already taken
            existing = session.query(User).filter(
                User.email == request.email,
                User.user_id != user_id
            ).first()
            
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already in use"
                )
            
            user.email = request.email
            user.is_verified = False
        
        if request.role_id is not None:
            # Verify role exists
            role = session.query(Role).filter(Role.role_id == request.role_id).first()
            if not role:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid role ID"
                )
            user.role_id = request.role_id
        
        if request.is_active is not None:
            user.is_active = request.is_active
        
        session.commit()
        session.refresh(user)
        
        logger.info(f"User updated by {current_user.username}: {user.username}")
        return user.to_dict()
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Update user error: {e}")
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update user"
        )


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session)
):
    """
    Delete a user (requires users.delete permission).
    
    Args:
        user_id: User ID to delete
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        Success message
    """
    # Check permission
    if not AuthService.check_permission(current_user.user_id, "users.delete"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: users.delete required"
        )
    
    # Prevent self-deletion
    if user_id == current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account"
        )
    
    # Get user
    user = session.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    try:
        username = user.username
        session.delete(user)
        session.commit()
        
        logger.info(f"User deleted by {current_user.username}: {username}")
        return {"message": "User deleted successfully"}
    
    except Exception as e:
        logger.error(f"Delete user error: {e}")
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete user"
        )


@router.put("/{user_id}/role")
async def change_user_role(
    user_id: str,
    request: ChangeUserRoleRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session)
):
    """
    Change user role (requires users.write permission).
    
    Args:
        user_id: User ID
        request: New role ID
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        Updated user information
    """
    # Check permission
    if not AuthService.check_permission(current_user.user_id, "users.write"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: users.write required"
        )
    
    # Get user
    user = session.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    # Verify role exists
    role = session.query(Role).filter(Role.role_id == request.role_id).first()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid role ID"
        )
    
    try:
        old_role_id = user.role_id
        user.role_id = request.role_id
        session.commit()
        session.refresh(user)

        # Invalidate the target user's existing tokens so the new
        # permissions take effect on their next request, not whenever their
        # cached token happens to expire.
        try:
            from backend.services.token_blacklist import revoke_all_for_user
            await revoke_all_for_user(user.user_id)
        except Exception as exc:
            logger.error(
                "Role changed for %s but revoke_all_for_user failed: %s. "
                "Old tokens may remain valid until natural expiry.",
                user.username,
                exc,
            )

        logger.info(f"User role changed by {current_user.username}: {user.username} from {old_role_id} to {request.role_id}")
        return user.to_dict()
    
    except Exception as e:
        logger.error(f"Change role error: {e}")
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to change user role"
        )


@router.get("/roles/list")
async def list_roles(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db_session)
):
    """
    List all available roles.
    
    Args:
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        List of roles
    """
    try:
        roles = session.query(Role).all()
        return {
            "roles": [role.to_dict() for role in roles]
        }
    
    except Exception as e:
        logger.error(f"List roles error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list roles"
        )

