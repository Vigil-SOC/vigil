"""
Authentication API - User authentication endpoints.

Handles login, logout, token refresh, password management, and MFA.
"""

import logging
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Header, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from backend.services.auth_cookies import (
    ACCESS_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    clear_auth_cookies,
    set_auth_cookies,
)
from backend.services.auth_service import AccountLockedError, AuthService
from backend.services.token_blacklist import (
    blacklist_jti,
    is_token_revoked,
    revoke_all_for_user,
)
from backend.middleware.auth import get_current_user, get_current_active_user
from backend.middleware.rate_limit import limiter
from database.models import User
from database.connection import get_db_session

logger = logging.getLogger(__name__)

router = APIRouter()


# Request/Response Models
class LoginRequest(BaseModel):
    """Login request."""
    username_or_email: str
    password: str
    mfa_code: Optional[str] = None


class LoginResponse(BaseModel):
    """Login response."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict


class ChangePasswordRequest(BaseModel):
    """Change password request."""
    current_password: str
    new_password: str


class RefreshTokenRequest(BaseModel):
    """Refresh token request.

    refresh_token is optional because the primary transport is now the
    HttpOnly refresh_token cookie. The body field stays for API/CLI
    clients that haven't migrated to the cookie flow.
    """
    refresh_token: Optional[str] = None


class MFASetupResponse(BaseModel):
    """MFA setup response."""
    secret: str
    qr_uri: str


class MFAVerifyRequest(BaseModel):
    """MFA verification request."""
    code: str


@router.post("/login", response_model=LoginResponse)
@limiter.limit("5/minute")
async def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    session: Session = Depends(get_db_session)
):
    """
    Authenticate user and issue tokens.

    Tokens are delivered two ways:
    - **HttpOnly cookies** (primary transport for the browser UI) — not
      readable from JavaScript, protected against XSS exfiltration.
    - **Response body** (for CLI/API clients) — these continue to send
      the token as `Authorization: Bearer …`.

    Args:
        request: FastAPI request (used by the rate limiter).
        response: FastAPI response, used to set auth cookies.
        payload: Login credentials
        session: Database session

    Returns:
        Access and refresh tokens with user info
    """
    # Authenticate user
    try:
        user = AuthService.authenticate_user(
            payload.username_or_email,
            payload.password,
            session
        )
    except AccountLockedError as exc:
        retry_after = max(1, int((exc.locked_until - datetime.utcnow()).total_seconds()))
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="Account locked due to repeated failed login attempts",
            headers={"Retry-After": str(retry_after)},
        )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username/email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check MFA if enabled
    if user.mfa_enabled:
        if not payload.mfa_code:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MFA code required",
                headers={"X-MFA-Required": "true"},
            )

        if not AuthService.verify_mfa_code(user.user_id, payload.mfa_code, session):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid MFA code",
            )

    # Generate tokens
    access_token = AuthService.generate_jwt_token(user, "access")
    refresh_token = AuthService.generate_jwt_token(user, "refresh")

    # Extract exp claims so the cookie Max-Age matches the JWT lifetime.
    access_payload = AuthService.verify_jwt_token(access_token) or {}
    refresh_payload = AuthService.verify_jwt_token(refresh_token) or {}
    set_auth_cookies(
        response,
        access_token,
        refresh_token,
        access_exp=access_payload.get("exp"),
        refresh_exp=refresh_payload.get("exp"),
    )

    logger.info(f"User logged in: {user.username}")

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=user.to_dict()
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_active_user),
    authorization: Optional[str] = Header(None),
):
    """
    Logout user — blacklist the current access token's JTI so replaying it
    returns 401 for the rest of its lifetime, and clear the HttpOnly auth
    cookies from the browser.

    Args:
        request: FastAPI request (used to read the access_token cookie).
        response: FastAPI response (used to clear auth cookies).
        current_user: Current authenticated user.
        authorization: Authorization header (used to extract the JTI for
            Bearer-flow clients).

    Returns:
        Success message.
    """
    # Prefer the cookie (browser flow). Fall back to Bearer for API clients.
    raw_token: Optional[str] = request.cookies.get(ACCESS_COOKIE_NAME)
    if not raw_token and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            raw_token = parts[1]

    if raw_token:
        payload = AuthService.verify_jwt_token(raw_token)
        if payload:
            jti = payload.get("jti")
            exp_ts = payload.get("exp")
            exp_dt = (
                datetime.utcfromtimestamp(exp_ts) if exp_ts is not None else None
            )
            if jti:
                try:
                    await blacklist_jti(jti, exp_dt)
                except Exception as exc:
                    # Redis down — logout still "succeeds" client-side
                    # (cookies cleared, client discards the Bearer token),
                    # but we surface the server-side failure for ops.
                    logger.error(
                        "Failed to blacklist token for %s: %s",
                        current_user.username,
                        exc,
                    )

    clear_auth_cookies(response)

    logger.info(f"User logged out: {current_user.username}")
    return {"message": "Logged out successfully"}


@router.post("/refresh", response_model=LoginResponse)
@limiter.limit("30/minute")
async def refresh_token(
    request: Request,
    response: Response,
    body: Optional[RefreshTokenRequest] = None,
    session: Session = Depends(get_db_session)
):
    """
    Refresh access token using refresh token.

    Token source priority:
    1. `refresh_token` HttpOnly cookie (browser flow)
    2. `refresh_token` field in the request body (Bearer-flow API clients)

    Args:
        request: FastAPI request (used by the rate limiter and to read
            the refresh_token cookie).
        response: FastAPI response (used to set the new auth cookies).
        body: Optional refresh-token body for API clients.
        session: Database session.

    Returns:
        New access and refresh tokens.
    """
    raw_refresh: Optional[str] = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_refresh and body is not None:
        raw_refresh = body.refresh_token
    if not raw_refresh:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token missing",
        )

    payload = AuthService.verify_jwt_token(raw_refresh)
    if not payload or payload.get("token_type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    if await is_token_revoked(payload):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
        )

    # Get user
    user = session.query(User).filter(User.user_id == payload["user_id"]).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )

    # Generate new tokens
    access_token = AuthService.generate_jwt_token(user, "access")
    refresh_token = AuthService.generate_jwt_token(user, "refresh")

    access_payload = AuthService.verify_jwt_token(access_token) or {}
    refresh_payload = AuthService.verify_jwt_token(refresh_token) or {}
    set_auth_cookies(
        response,
        access_token,
        refresh_token,
        access_exp=access_payload.get("exp"),
        refresh_exp=refresh_payload.get("exp"),
    )

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=user.to_dict()
    )


@router.get("/me")
async def get_current_user_info(current_user: User = Depends(get_current_active_user)):
    """
    Get current user information.
    
    Args:
        current_user: Current authenticated user
    
    Returns:
        User information with permissions
    """
    user_dict = current_user.to_dict()
    
    # Add permissions
    permissions = AuthService.get_user_permissions(current_user.user_id)
    user_dict["permissions"] = permissions
    
    return user_dict


@router.put("/me")
async def update_current_user(
    full_name: Optional[str] = None,
    email: Optional[EmailStr] = None,
    current_user: User = Depends(get_current_active_user),
    session: Session = Depends(get_db_session)
):
    """
    Update current user profile.
    
    Args:
        full_name: New full name
        email: New email
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        Updated user information
    """
    try:
        if full_name:
            current_user.full_name = full_name
        
        if email:
            # Check if email is already taken
            existing = session.query(User).filter(
                User.email == email,
                User.user_id != current_user.user_id
            ).first()
            
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Email already in use"
                )
            
            current_user.email = email
            current_user.is_verified = False  # Require re-verification
        
        session.commit()
        session.refresh(current_user)
        
        logger.info(f"User profile updated: {current_user.username}")
        return current_user.to_dict()
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Profile update error: {e}")
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update profile"
        )


@router.post("/change-password")
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_active_user),
    session: Session = Depends(get_db_session)
):
    """
    Change user password.

    Args:
        request: FastAPI request (used by the rate limiter).
        body: Current and new password
        current_user: Current authenticated user
        session: Database session

    Returns:
        Success message
    """
    # Verify current password
    if not AuthService.verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect"
        )

    # Validate new password
    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must be at least 8 characters"
        )

    try:
        # Update password
        current_user.password_hash = AuthService.hash_password(body.new_password)
        session.commit()

        # Invalidate every outstanding token for this user. The current
        # session is effectively logged out; the client should re-login.
        try:
            await revoke_all_for_user(current_user.user_id)
        except Exception as exc:
            logger.error(
                "Password changed for %s but revoke_all_for_user failed: %s. "
                "Old tokens may remain valid until natural expiry.",
                current_user.username,
                exc,
            )

        logger.info(f"Password changed for user: {current_user.username}")
        return {"message": "Password changed successfully"}
    
    except Exception as e:
        logger.error(f"Password change error: {e}")
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to change password"
        )


@router.post("/mfa/setup", response_model=MFASetupResponse)
async def setup_mfa(
    current_user: User = Depends(get_current_active_user),
    session: Session = Depends(get_db_session)
):
    """
    Setup MFA for current user.
    
    Args:
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        MFA secret and QR code URI
    """
    secret = AuthService.setup_mfa(current_user.user_id, session)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to setup MFA"
        )
    
    qr_uri = AuthService.get_mfa_qr_uri(current_user.user_id, session)
    if not qr_uri:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate QR code"
        )
    
    return MFASetupResponse(secret=secret, qr_uri=qr_uri)


@router.post("/mfa/verify")
async def verify_mfa(
    request: MFAVerifyRequest,
    current_user: User = Depends(get_current_active_user),
    session: Session = Depends(get_db_session)
):
    """
    Verify MFA code and enable MFA.
    
    Args:
        request: MFA code
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        Success message
    """
    is_valid = AuthService.verify_mfa_code(current_user.user_id, request.code, session)
    
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid MFA code"
        )
    
    return {"message": "MFA enabled successfully"}


@router.delete("/mfa")
async def disable_mfa(
    current_user: User = Depends(get_current_active_user),
    session: Session = Depends(get_db_session)
):
    """
    Disable MFA for current user.
    
    Args:
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        Success message
    """
    try:
        current_user.mfa_enabled = False
        current_user.mfa_secret = None
        session.commit()
        
        logger.info(f"MFA disabled for user: {current_user.username}")
        return {"message": "MFA disabled successfully"}
    
    except Exception as e:
        logger.error(f"MFA disable error: {e}")
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to disable MFA"
        )


# Public self-registration was removed intentionally. All user creation
# goes through the admin-gated POST /api/users/ endpoint (backend/api/users.py)
# which validates the requested role against the caller's privileges.


