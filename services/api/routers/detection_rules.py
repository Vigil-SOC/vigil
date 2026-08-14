"""Detection Rules API endpoints for managing detection rule sources."""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.deps import provide_detection_rules, provide_mcp_client
from core.detections.detection_rules_service import DetectionRulesService
from core.routing import Auth, RouterMeta

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/detection-rules",
    tags=["detection-rules"],
    auth=Auth.REQUIRED,
)


class AddSourceRequest(BaseModel):
    """Request to add a new detection rule source."""

    name: str
    source_type: str  # 'git' or 'local'
    format: str  # 'sigma', 'splunk', 'elastic', 'kql', 'auto'
    url: Optional[str] = None
    path: Optional[str] = None
    subdirectory: str = ""
    story_subdirectory: str = ""


@router.get("/sources")
async def list_sources(
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """
    List all registered detection rule sources.

    Returns:
        List of sources with metadata (name, format, rule count, status, etc.)
    """
    try:
        sources = service.list_sources()
        return {"sources": sources, "count": len(sources)}
    except Exception as e:
        logger.error(f"Error listing detection sources: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sources/{source_id}")
async def get_source(
    source_id: str,
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """
    Get details for a specific detection rule source.

    Args:
        source_id: The source ID

    Returns:
        Source details
    """
    try:
        source = service.get_source(source_id)
        if not source:
            raise HTTPException(
                status_code=404, detail=f"Source not found: {source_id}"
            )
        return source
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting source: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sources")
async def add_source(
    request: AddSourceRequest,
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """
    Add a new detection rule source (git repo or local directory).

    Args:
        request: Source configuration (name, type, format, url/path, etc.)

    Returns:
        The newly created source
    """
    try:
        source = service.add_source(
            name=request.name,
            source_type=request.source_type,
            format=request.format,
            url=request.url,
            path=request.path,
            subdirectory=request.subdirectory,
            story_subdirectory=request.story_subdirectory,
        )
        return {"success": True, "source": source}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error adding source: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/sources/{source_id}")
async def remove_source(
    source_id: str,
    delete_files: bool = False,
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """
    Remove a detection rule source.

    Args:
        source_id: The source ID to remove
        delete_files: Whether to delete the cloned files on disk

    Returns:
        Success status
    """
    try:
        success = service.remove_source(source_id, delete_files=delete_files)
        if not success:
            raise HTTPException(
                status_code=404, detail=f"Source not found: {source_id}"
            )
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error removing source: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sources/{source_id}/update")
async def update_source(
    source_id: str,
    service: DetectionRulesService = Depends(provide_detection_rules),
    mcp_client=Depends(provide_mcp_client),
):
    """
    Update a single detection rule source (git pull or rescan).

    Args:
        source_id: The source ID to update

    Returns:
        Updated source details
    """
    try:
        source = service.update_source(source_id)

        # After updating, restart the security-detections MCP server to rebuild index
        await _restart_security_detections_mcp(mcp_client, service)

        return {"success": True, "source": source}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating source: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/update-all")
async def update_all_sources(
    service: DetectionRulesService = Depends(provide_detection_rules),
    mcp_client=Depends(provide_mcp_client),
):
    """
    Update all detection rule sources (git pull all repos).

    Returns:
        Results for each source update
    """
    try:
        results = service.update_all()

        # After updating all, restart the security-detections MCP server
        await _restart_security_detections_mcp(mcp_client, service)

        return {"success": True, "results": results}
    except Exception as e:
        logger.error(f"Error updating all sources: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats")
async def get_stats(
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """
    Get aggregate detection rule statistics.

    Returns:
        Statistics including total rules, breakdown by format, and per-source counts
    """
    try:
        stats = service.get_stats()
        return stats
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mcp-env")
async def get_mcp_env(
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """
    Get the environment variables that would be passed to the Security-Detections-MCP server.

    Returns:
        Dictionary of environment variable names to their values
    """
    try:
        env_vars = service.get_mcp_env_vars()
        return {"env_vars": env_vars}
    except Exception as e:
        logger.error(f"Error getting MCP env vars: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/reload")
async def reload_service(
    service: DetectionRulesService = Depends(provide_detection_rules),
    mcp_client=Depends(provide_mcp_client),
):
    """
    Reload the detection rules service (re-reads config and rescans all sources).
    Also restarts the security-detections MCP server.

    Returns:
        Success status with updated stats
    """
    try:

        # Re-read config
        service._load_config()

        # Rescan all sources
        for source in service.sources:
            from pathlib import Path

            source["rule_count"] = service._count_rules(
                Path(source["local_path"]),
                source["format"],
                source.get("subdirectory", ""),
            )
            if Path(source["local_path"]).exists():
                source["status"] = "ready"
        service._save_config()

        # Restart the MCP server
        await _restart_security_detections_mcp(mcp_client, service)

        stats = service.get_stats()
        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"Error reloading service: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _restart_security_detections_mcp(mcp_client, service: DetectionRulesService):
    """
    Restart the security-detections MCP server to pick up new/updated rule sources.
    This triggers a re-index of all detection rules in the MCP server.
    """
    try:
        if mcp_client and mcp_client.mcp_service:
            mcp_service = mcp_client.mcp_service
            server_name = "security-detections"

            if server_name in mcp_service.servers:
                # Update the server's env vars with latest paths from detection_rules_service
                env_vars = service.get_mcp_env_vars()

                server = mcp_service.servers[server_name]
                server.env.update(env_vars)

                # Stop and restart
                mcp_service.stop_server(server_name)

                # Disconnect and reconnect MCP client
                await mcp_client.disconnect_from_server(server_name)
                await mcp_client.connect_to_server(server_name, persistent=True)

                logger.info(f"Restarted {server_name} MCP server with updated env vars")
            else:
                logger.warning(f"MCP server '{server_name}' not found in service")
    except Exception as e:
        logger.warning(f"Could not restart security-detections MCP: {e}")
