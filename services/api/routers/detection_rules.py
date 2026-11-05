"""Detection Rules API endpoints for managing detection rule sources."""

import asyncio
import logging
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel

from core.auth.permissions import permission_gate
from core.deps import provide_detection_rules, provide_mcp_client, provide_mcp_registry
from core.detections.detection_rules_service import DetectionRulesService
from core.detections.inventory import catalog
from core.detections.inventory.sync import sync_repos
from core.integrations.mcp.registry import MCPRegistry, register_connected
from core.routing import Auth, RouterMeta

logger = logging.getLogger(__name__)

router = APIRouter()

# Sources are what the security-detections server indexes, and reload restarts it.
_SETTINGS_WRITE = [permission_gate("detections.write")]
router.dependencies.append(permission_gate("detections.read"))

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
    deploy_target: bool = False


class DeployTargetRequest(BaseModel):
    """Whether this git source receives Vigil's live rules of its format."""

    deploy_target: bool


@router.get("/sources")
def list_sources(
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """
    List all registered detection rule sources.

    Returns:
        List of sources with metadata (name, format, rule count, status, etc.)
    """
    sources = service.list_sources()
    return {"sources": sources, "count": len(sources)}


@router.get("/sources/{source_id}")
def get_source(
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
    source = service.get_source(source_id)
    if not source:
        raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")
    return source


@router.post("/sources", dependencies=_SETTINGS_WRITE)
def add_source(
    request: AddSourceRequest,
    background: BackgroundTasks,
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
            deploy_target=request.deploy_target,
        )
        background.add_task(_sync_detection_rules)
        return {"success": True, "source": source}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/sources/{source_id}", dependencies=_SETTINGS_WRITE)
def remove_source(
    source_id: str,
    background: BackgroundTasks,
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
    success = service.remove_source(source_id, delete_files=delete_files)
    if not success:
        raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")
    background.add_task(_sync_detection_rules)
    return {"success": True}


@router.post("/sources/{source_id}/update", dependencies=_SETTINGS_WRITE)
async def update_source(
    source_id: str,
    background: BackgroundTasks,
    service: DetectionRulesService = Depends(provide_detection_rules),
    mcp_client=Depends(provide_mcp_client),
    registry: MCPRegistry = Depends(provide_mcp_registry),
):
    """
    Update a single detection rule source (git pull or rescan).

    Args:
        source_id: The source ID to update

    Returns:
        Updated source details
    """
    try:
        # git pull runs up to 120s; keep it off the event loop.
        source = await asyncio.to_thread(service.update_source, source_id)

        # After updating, restart the security-detections MCP server to rebuild index
        await _restart_security_detections_mcp(mcp_client, service, registry)
        background.add_task(_sync_detection_rules)

        return {"success": True, "source": source}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/sources/{source_id}/deploy-target", dependencies=_SETTINGS_WRITE)
def set_deploy_target(
    source_id: str,
    request: DeployTargetRequest,
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """Mark or unmark a git source as where Vigil's live rules of its format go."""
    try:
        source = service.set_deploy_target(source_id, request.deploy_target)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "source": source}


@router.post("/update-all", dependencies=_SETTINGS_WRITE)
async def update_all_sources(
    background: BackgroundTasks,
    service: DetectionRulesService = Depends(provide_detection_rules),
    mcp_client=Depends(provide_mcp_client),
    registry: MCPRegistry = Depends(provide_mcp_registry),
):
    """
    Update all detection rule sources (git pull all repos).

    Returns:
        Results for each source update
    """
    results = await asyncio.to_thread(service.update_all)

    # After updating all, restart the security-detections MCP server
    await _restart_security_detections_mcp(mcp_client, service, registry)
    background.add_task(_sync_detection_rules)

    return {"success": True, "results": results}


@router.get("/stats")
def get_stats(
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """
    Get aggregate detection rule statistics.

    Returns:
        Statistics including total rules, breakdown by format, and per-source counts
    """
    stats = service.get_stats()
    return stats


@router.get("/health")
def get_health(limit: int = Query(200, ge=1, le=1000)):
    """Whether each SIEM's rules can fire: per-tool check tallies, Blind spots,
    the rules failing or unknown on a check (fails first, capped at ``limit``),
    and the repo rules no SIEM has a copy of. Read-only."""
    by_tool = catalog.rules_by_tool()
    siems = [t for t in by_tool if t not in ("repo", catalog.OVERLAY)]
    return {
        "by_tool": by_tool,
        "unchecked": catalog.unchecked(siems),
        "not_read": catalog.NOT_READ,
        "blind_spots": catalog.blind_spots(),
        "not_passing": catalog.rules_not_passing(limit),
        "repo": catalog.repo_coverage(),
    }


@router.get("/overlay-only")
def get_overlay_only():
    """Live overlay copies no SIEM rule runs: only Vigil detects with these,
    so Stop halts their detection. Read-only."""
    from core.detections.inventory import kibana, rollout

    live = [c for c in rollout._copies() if c.stage == "live"]
    gone = rollout.siem_runs(kibana.SOURCE, [c.rule_id for c in live])
    return {
        "rules": [
            {"id": c.rule_id, "name": c.name, "language": c.rule.get("language")}
            for c in live
            if c.rule_id not in gone
        ]
    }


@router.get("/mcp-env")
def get_mcp_env(
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """
    Get the environment variables that would be passed to the Security-Detections-MCP server.

    Returns:
        Dictionary of environment variable names to their values
    """
    env_vars = service.get_mcp_env_vars()
    return {"env_vars": env_vars}


@router.post("/reload", dependencies=_SETTINGS_WRITE)
async def reload_service(
    service: DetectionRulesService = Depends(provide_detection_rules),
    mcp_client=Depends(provide_mcp_client),
    registry: MCPRegistry = Depends(provide_mcp_registry),
):
    """
    Reload the detection rules service (re-reads config and rescans all sources),
    syncs every source into the detection rule tables, and restarts the
    security-detections MCP server.

    Returns:
        Success status with updated stats and what the sync wrote per source
    """
    await asyncio.to_thread(service.reload)
    reports = await asyncio.to_thread(sync_repos)

    # Restart the MCP server
    await _restart_security_detections_mcp(mcp_client, service, registry)

    stats = await asyncio.to_thread(service.get_stats)
    return {
        "success": True,
        "stats": stats,
        "sync": [asdict(report) for report in reports],
    }


def _sync_detection_rules() -> None:
    """Sync after a source changed. Runs after the response; a failure is only logged."""
    try:
        sync_repos()
    except Exception as e:  # noqa: BLE001 -- the hourly daemon sync retries
        logger.warning(f"Detection rule sync failed: {e}")


async def _restart_security_detections_mcp(
    mcp_client, service: DetectionRulesService, registry: MCPRegistry
):
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

                # Restart, not disable: a failed reconnect must not hide tools.
                await mcp_client.disconnect_from_server(server_name)
                reconnected = await mcp_client.connect_to_server(
                    server_name, persistent=True
                )
                if reconnected:
                    try:
                        register_connected(registry, mcp_client, server_name)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "MCP registry register during restart failed for %s: %s",
                            server_name,
                            exc,
                        )

                logger.info(f"Restarted {server_name} MCP server with updated env vars")
            else:
                logger.warning(f"MCP server '{server_name}' not found in service")
    except Exception as e:
        logger.warning(f"Could not restart security-detections MCP: {e}")
