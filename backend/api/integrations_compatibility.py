"""API endpoints for integration compatibility checking and management.

Package install/upgrade/uninstall is driven by a server-side allowlist of
known integration IDs — the wire never carries a raw package name. This
prevents the unauthenticated-RCE chain disclosed 2026-05 where the old
``package_name`` body field flowed straight into ``pip install``.

All mutating endpoints require an authenticated admin
(``integrations.write`` permission).
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import logging

from backend.middleware.auth import get_current_active_user
from backend.services.auth_service import AuthService
from database.models import User
from services.integration_compatibility_service import get_compatibility_service

router = APIRouter()
logger = logging.getLogger(__name__)


class IntegrationActionRequest(BaseModel):
    """Request body for install/upgrade/uninstall of a known integration."""

    integration_id: str


def _require_integrations_admin(current_user: User) -> None:
    """Raise 403 unless the user has ``integrations.write``.

    Centralised so all three mutating endpoints share the exact same
    check.
    """
    if not AuthService.check_permission(current_user.user_id, "integrations.write"):
        raise HTTPException(
            status_code=403,
            detail="Permission denied: integrations.write required",
        )


@router.get("/compatibility/status")
async def get_compatibility_status(
    current_user: User = Depends(get_current_active_user),
):
    """Get compatibility status for all integrations."""
    try:
        service = get_compatibility_service()
        statuses = service.get_all_statuses()
        system_info = service.get_system_info()

        return {
            "system": system_info,
            "integrations": statuses,
        }
    except Exception as e:
        logger.error(f"Error getting compatibility status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/compatibility/status/{integration_id}")
async def get_integration_compatibility(
    integration_id: str,
    current_user: User = Depends(get_current_active_user),
):
    """Get compatibility status for a specific integration."""
    try:
        service = get_compatibility_service()
        status = service.get_integration_status(integration_id)

        if status.get("status") == "unknown":
            raise HTTPException(
                status_code=404,
                detail=f"Integration '{integration_id}' not found",
            )

        return status
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting integration compatibility: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/compatibility/install")
async def install_package(
    request: IntegrationActionRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Install or upgrade the pinned package for a known integration.

    The request body is ``{"integration_id": "..."}`` — the server
    looks up the package name and minimum version in its own
    integration registry. There is no way for the client to specify
    a package name, URL, or version directly.
    """
    _require_integrations_admin(current_user)

    service = get_compatibility_service()
    allowed = service.get_allowed_integration_ids()
    if request.integration_id not in allowed:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Integration '{request.integration_id}' is not installable. "
                "See /compatibility/status for the allowed list."
            ),
        )

    logger.info(
        "User %s requested install of integration %s",
        current_user.user_id,
        request.integration_id,
    )

    try:
        success, message = service.install_known_integration(request.integration_id)
        if success:
            return {
                "success": True,
                "message": message,
                "integration_id": request.integration_id,
            }
        raise HTTPException(status_code=500, detail=message)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error installing integration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/compatibility/upgrade")
async def upgrade_package(
    request: IntegrationActionRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Upgrade an integration's pinned package."""
    _require_integrations_admin(current_user)

    service = get_compatibility_service()
    if request.integration_id not in service.get_allowed_integration_ids():
        raise HTTPException(
            status_code=404,
            detail=f"Integration '{request.integration_id}' is not installable",
        )

    logger.info(
        "User %s requested upgrade of integration %s",
        current_user.user_id,
        request.integration_id,
    )

    try:
        success, message = service.upgrade_known_integration(request.integration_id)
        if success:
            return {
                "success": True,
                "message": message,
                "integration_id": request.integration_id,
            }
        raise HTTPException(status_code=500, detail=message)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error upgrading integration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/compatibility/uninstall")
async def uninstall_package(
    request: IntegrationActionRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Uninstall the package backing a known integration."""
    _require_integrations_admin(current_user)

    service = get_compatibility_service()
    if request.integration_id not in service.get_allowed_integration_ids():
        raise HTTPException(
            status_code=404,
            detail=f"Integration '{request.integration_id}' is not installable",
        )

    logger.info(
        "User %s requested uninstall of integration %s",
        current_user.user_id,
        request.integration_id,
    )

    try:
        success, message = service.uninstall_known_integration(request.integration_id)
        if success:
            return {
                "success": True,
                "message": message,
                "integration_id": request.integration_id,
            }
        raise HTTPException(status_code=500, detail=message)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error uninstalling integration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/compatibility/system")
async def get_system_info(
    current_user: User = Depends(get_current_active_user),
):
    """Get system information including Python version."""
    try:
        service = get_compatibility_service()
        return service.get_system_info()
    except Exception as e:
        logger.error(f"Error getting system info: {e}")
        raise HTTPException(status_code=500, detail=str(e))
