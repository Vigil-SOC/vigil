"""Custom Integration Builder API - AI-powered integration generation.

All mutating routes require an authenticated admin
(``integrations.write`` permission). ``/save`` accepts a JSON body
only — the previous query-string-based shape allowed traversal payloads
to be smuggled through ``--url-query`` (see 2026-05 disclosure).
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from pathlib import Path
import json
import logging
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from backend.middleware.auth import get_current_active_user
from backend.services.auth_service import AuthService
from database.models import User
from services.custom_integration_service import (
    CustomIntegrationService,
    InvalidIntegrationIdError,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _require_integrations_admin(current_user: User) -> None:
    """Raise 403 unless the user has ``integrations.write``."""
    if not AuthService.check_permission(current_user.user_id, "integrations.write"):
        raise HTTPException(
            status_code=403,
            detail="Permission denied: integrations.write required",
        )


class CustomIntegrationRequest(BaseModel):
    """Request to generate a custom integration from documentation."""

    documentation: str
    integration_name: Optional[str] = None
    category: Optional[str] = "Custom"
    conversation_history: Optional[list] = None
    user_response: Optional[str] = None


class SaveIntegrationRequest(BaseModel):
    """Body schema for ``POST /save``.

    Mandatory JSON body — does not accept query-string fallback. The
    previous shape took the same fields as raw path/query args and was
    abused to overwrite ``mempalace/mempalace/mcp_server.py``.
    """

    integration_id: str
    metadata: dict
    server_code: str


class CustomIntegrationResponse(BaseModel):
    """Response containing generated integration details."""

    success: bool
    needs_clarification: Optional[bool] = False
    integration_id: Optional[str] = None
    integration_name: Optional[str] = None
    metadata: Optional[dict] = None
    server_code: Optional[str] = None
    message: Optional[str] = None
    conversation_history: Optional[list] = None
    error: Optional[str] = None


@router.post("/generate")
async def generate_custom_integration(
    request: CustomIntegrationRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Generate a custom integration from API/MCP documentation using Claude AI."""
    _require_integrations_admin(current_user)
    try:
        service = CustomIntegrationService()

        # If there's a user response, add it to the conversation
        conversation_history = request.conversation_history or []
        if request.user_response:
            conversation_history.append(
                {"role": "user", "content": request.user_response}
            )

        # Generate the integration
        result = await service.generate_integration(
            documentation=request.documentation,
            integration_name=request.integration_name,
            category=request.category,
            conversation_history=conversation_history if conversation_history else None,
        )

        if not result["success"]:
            raise HTTPException(
                status_code=500,
                detail=result.get("error", "Failed to generate integration"),
            )

        # Return the result as-is (let FastAPI handle the dict -> JSON conversion)
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating custom integration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/generate/upload")
async def generate_from_file(
    file: UploadFile = File(...),
    integration_name: Optional[str] = Form(None),
    category: Optional[str] = Form("Custom"),
    current_user: User = Depends(get_current_active_user),
):
    """Generate a custom integration from an uploaded documentation file."""
    _require_integrations_admin(current_user)
    try:
        # Read file content
        content = await file.read()

        # Try to decode as text
        try:
            documentation = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                documentation = content.decode("latin-1")
            except Exception as e:
                logger.error(f"Failed to decode uploaded file: {e}")
                raise HTTPException(
                    status_code=400,
                    detail="Unable to decode file. Please upload a text-based document.",
                )

        # Generate integration
        service = CustomIntegrationService()
        result = await service.generate_integration(
            documentation=documentation,
            integration_name=integration_name,
            category=category,
        )

        if not result["success"]:
            raise HTTPException(
                status_code=500,
                detail=result.get("error", "Failed to generate integration"),
            )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating integration from file: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save")
async def save_custom_integration(
    request: SaveIntegrationRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Save a generated custom integration to the system.

    Body is JSON only. The service-layer validates ``integration_id``
    against a strict allowlist regex and resolves the final write path
    inside the custom-integrations directory before any disk I/O.
    """
    _require_integrations_admin(current_user)

    try:
        service = CustomIntegrationService()
        result = await service.save_integration(
            integration_id=request.integration_id,
            metadata=request.metadata,
            server_code=request.server_code,
        )

        if not result["success"]:
            # Treat validation errors as 400 so the caller can fix the
            # request; treat anything else as a 500.
            error = result.get("error", "Failed to save integration")
            status_code = (
                400 if "integration_id" in error or "server_code" in error else 500
            )
            raise HTTPException(status_code=status_code, detail=error)

        return result

    except HTTPException:
        raise
    except InvalidIntegrationIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error saving custom integration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/list")
async def list_custom_integrations(
    current_user: User = Depends(get_current_active_user),
):
    """List all custom integrations.

    Admin-gated because the listing leaks integration metadata
    (descriptions, configuration shape) that's useful for an attacker
    profiling the deployment.
    """
    _require_integrations_admin(current_user)
    try:
        service = CustomIntegrationService()
        custom_integrations = service.list_custom_integrations()

        return {"success": True, "integrations": custom_integrations}

    except Exception as e:
        logger.error(f"Error listing custom integrations: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{integration_id}")
async def delete_custom_integration(
    integration_id: str,
    current_user: User = Depends(get_current_active_user),
):
    """Delete a custom integration."""
    _require_integrations_admin(current_user)
    try:
        service = CustomIntegrationService()
        result = await service.delete_integration(integration_id)

        if not result["success"]:
            error = result.get("error", "Integration not found")
            status_code = 400 if "integration_id" in error else 404
            raise HTTPException(status_code=status_code, detail=error)

        return result

    except HTTPException:
        raise
    except InvalidIntegrationIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error deleting custom integration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{integration_id}/validate")
async def validate_custom_integration(
    integration_id: str,
    current_user: User = Depends(get_current_active_user),
):
    """Validate a custom integration's server code."""
    _require_integrations_admin(current_user)
    try:
        service = CustomIntegrationService()
        result = await service.validate_integration(integration_id)

        return result

    except InvalidIntegrationIdError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error validating custom integration: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
