"""Detection Rules API endpoints for managing detection rule sources."""

import asyncio
import logging
from dataclasses import asdict
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from core.auth.permissions import permission_gate
from core.deps import provide_detection_rules, provide_mcp_client, provide_mcp_registry
from core.detections import exceptions, proof_card
from core.detections.detection_rules_service import (
    DEPLOY_FORMATS,
    DetectionRulesService,
    is_vendor_repo,
)
from core.detections.inventory import catalog, health_inbox, stages
from core.detections.inventory.sync import sync_repos
from core.integrations.mcp.registry import MCPRegistry, register_connected
from core.integrations.mcp.surface import acting_as
from core.routing import Auth, RouterMeta
from core.storage.config_service import get_config_service
from core.storage.models import User
from services.api.middleware.auth import get_current_active_user

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
    # The person says this non-github.com host is GitHub Enterprise.
    github_enterprise: bool = False


def _audit_deploy_target(user: User, source: dict, old: bool, new: bool) -> None:
    """Record a deploy-target change. Best-effort: never fails the call."""
    try:
        get_config_service(user_id=str(user.user_id)).record_audit(
            config_type="detection_rules",
            config_key=f"deploy_target:{source['id']}",
            action="update" if new else "delete",
            old_value={"deploy_target": old},
            new_value={
                "source_id": source["id"],
                "name": source.get("name"),
                "format": source.get("format"),
                "git_url": source.get("git_url"),
                "github_enterprise": bool(source.get("github_enterprise")),
                "deploy_target": new,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not write deploy target audit log: %s", e)


class DeployTargetRequest(BaseModel):
    """Whether this git source receives Vigil's live rules of its format."""

    deploy_target: bool
    # With deploy_target on: the person says the non-github.com host is GitHub
    # Enterprise. Omitted keeps what is saved.
    github_enterprise: Optional[bool] = None


@router.get("/sources")
def list_sources(
    service: DetectionRulesService = Depends(provide_detection_rules),
):
    """
    List all registered detection rule sources.

    Returns:
        List of sources with metadata (name, format, rule count, status, etc.)
    """
    # vendor_repo: a shipped vendor repo, which can never be a deploy target.
    # deploy_target is the one rollout honours per format, so a saved file
    # holding two flags of one format never shows both as on.
    honoured = {
        target["id"]
        for fmt in DEPLOY_FORMATS
        if (target := service.deploy_target(fmt)) is not None
    }
    sources = [
        {
            **s,
            "vendor_repo": is_vendor_repo(s.get("git_url", "")),
            "deploy_target": s.get("id") in honoured,
        }
        for s in service.list_sources()
    ]
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
    current_user: User = Depends(get_current_active_user),
):
    """
    Add a new detection rule source (git repo or local directory).

    Args:
        request: Source configuration (name, type, format, url/path, etc.)

    Returns:
        The newly created source
    """
    try:
        before = (
            service.deploy_target(request.format) if request.deploy_target else None
        )
        source = service.add_source(
            name=request.name,
            source_type=request.source_type,
            format=request.format,
            url=request.url,
            path=request.path,
            subdirectory=request.subdirectory,
            story_subdirectory=request.story_subdirectory,
            deploy_target=request.deploy_target,
            github_enterprise=request.github_enterprise,
        )
        if request.deploy_target:
            was = before is not None and before.get("id") == source["id"]
            _audit_deploy_target(current_user, source, was, True)
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
    current_user: User = Depends(get_current_active_user),
):
    """Mark or unmark a git source as where Vigil's live rules of its format go."""
    old = bool((service.get_source(source_id) or {}).get("deploy_target"))
    try:
        source = service.set_deploy_target(
            source_id, request.deploy_target, request.github_enterprise
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _audit_deploy_target(current_user, source, old, request.deploy_target)
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


@router.get("/rules")
def get_rules(
    health: Optional[
        Literal[
            "attention",
            "fails_to_run",
            "data_silent",
            "missing_field",
            "never_fired",
            "fires",
            "disabled",
            "no_fire_data",
        ]
    ] = None,
    tool: Optional[Literal["elastic", "splunk", "wazuh"]] = None,
    q: Optional[str] = Query(default=None, max_length=200),
    offset: int = Query(default=0, ge=0),
    sort: Literal["worst", "most_fires", "fewest_fires", "name"] = "worst",
    flag: Optional[
        Literal["noisy", "quiet", "accurate", "silent", "drift", "caught"]
    ] = None,
    rules: Optional[str] = Query(
        default=None, max_length=4000, pattern=r"^[0-9]{1,12}(,[0-9]{1,12})*$"
    ),
    has_exceptions: bool = False,
):
    """Every listed rule copy's health with its reason and data source, capped at
    catalog.RULE_ROWS rows from ``offset``, narrowed by ``health`` ("attention"
    is every state to act on), ``tool``, ``flag`` (a failing Noisy, Quiet,
    Accurate, Silent or drift check, or a passing Caught), the search ``q`` and
    ``rules`` (comma-separated rule ids, as a rule-health entry links them), in
    ``sort`` order (worst first by default); with ``has_exceptions``, every copy
    with an active exception whatever its health. The counts, the per-SIEM
    tallies and the blind spots cover them all. Read-only."""
    rule_ids = [int(i) for i in rules.split(",")] if rules else None
    return catalog.health_listing(
        health,
        tool=tool,
        q=q,
        offset=offset,
        sort=sort,
        flag=flag,
        rule_ids=rule_ids,
        has_exceptions=has_exceptions,
    )


class HealthReadRequest(BaseModel):
    """Which rule-health entries to mark read: ``change_ids``, or every open one."""

    change_ids: Optional[List[int]] = Field(default=None, max_length=1000)
    all: bool = False


@router.get("/health-inbox")
def get_health_inbox(user: User = Depends(get_current_active_user)):
    """The open rule-health flags, newest first, each read or unread for the
    signed-in person, with a summary when many are unread. Read-only."""
    return health_inbox.inbox(user.user_id)


@router.post("/health-inbox/read")
def read_health_inbox(
    body: HealthReadRequest, user: User = Depends(get_current_active_user)
):
    """Marks entries read for the signed-in person only; returns the inbox."""
    if body.all == (body.change_ids is not None):
        raise HTTPException(status_code=422, detail="Send change_ids or all, not both.")
    return health_inbox.mark_read(user.user_id, None if body.all else body.change_ids)


@router.get("/overlay-only")
def get_overlay_only():
    """Live overlay copies no SIEM rule runs: only Vigil detects with these,
    so Stop halts their detection. Read-only."""
    from core.detections.inventory import rollout

    return {"rules": rollout.overlay_only()}


class DeployOverlayRequest(BaseModel):
    """Which live overlay-only rules to deploy; none means all of them."""

    rule_ids: Optional[List[int]] = None


@router.post("/overlay-only/deploy", dependencies=_SETTINGS_WRITE)
def deploy_overlay_only(
    body: DeployOverlayRequest,
    user: User = Depends(get_current_active_user),
):
    """Open the deploy pull request now, as the rollout sweep would, into the
    deploy target for each live rule only Vigil runs (these, or all); a failed
    one is tried again. Says what each came to and why others were skipped."""
    from core.detections.inventory import rollout

    # Each outcome's trail row is written with its checks['deploy'], as this user.
    return rollout.deploy_overlay_only(body.rule_ids, actor=user.username)


class AddExceptionRequest(BaseModel):
    """A customer-specific exception: every {field: value} pair must match."""

    match: Dict[str, Any]
    note: str


def _edited(result: Dict[str, Any]) -> Dict[str, Any]:
    """``edit_exception``'s result, its error as a 4xx carrying the same text."""
    error = result.get("error")
    if not error:
        return result
    if error.startswith(("No detection rule", "No exception")):
        status = 404
    elif "already" in error:
        status = 409
    else:
        status = 400
    raise HTTPException(status_code=status, detail=error)


@router.get("/rules/{rule_id}/stages")
def get_rule_stages(rule_id: int):
    """A rule's stage rows, newest first: version, from and to stage, actor,
    reason and time. Read-only."""
    try:
        return {"rule_id": rule_id, "stages": stages.history(rule_id)}
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))


class StageChangeRequest(BaseModel):
    """Why a person reverts a rule, or releases it."""

    reason: str = Field(min_length=1, max_length=2000)


def _stage_change(change, rule_id: int, body: StageChangeRequest, user: User):
    from core.agents.attribution import principal_id_for_username

    who = (user.username, principal_id_for_username(user.username) or user.username)
    try:
        return change(rule_id, who, body.reason.strip())
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/rules/{rule_id}/revert", dependencies=_SETTINGS_WRITE)
def revert_rule(
    rule_id: int,
    body: StageChangeRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Moves a live rule back to shadow and holds it there: the rollout does
    not promote it again until a person releases it."""
    return _stage_change(stages.revert, rule_id, body, current_user)


@router.post("/rules/{rule_id}/release", dependencies=_SETTINGS_WRITE)
def release_rule(
    rule_id: int,
    body: StageChangeRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Lifts a person's hold on a shadow rule; the normal rollout applies again."""
    return _stage_change(stages.release, rule_id, body, current_user)


@router.get("/rules/{rule_id}/findings")
def get_rule_findings(rule_id: int):
    """The findings a rule copy fired in the fires window, newest first, as the
    rules list counts them. Read-only."""
    return catalog.rule_findings(rule_id)


class ProofCardRequest(BaseModel):
    """What to export: these rules, or (none) every live rule with coverage."""

    rule_ids: Optional[List[int]] = Field(
        default=None, min_length=1, max_length=proof_card.MAX_RULES
    )


@router.post("/proof-card")
def export_proof_card(
    body: ProofCardRequest, user: User = Depends(get_current_active_user)
):
    """A zip of the proof card: proof-card.json (proof-card/v1), its Ed25519
    signature, a PDF of the same data, and the public key. Read-only on the
    inventory; makes the instance's signing key on first use. The trail records
    each export, and the key's making, as this user."""
    try:
        card, data = proof_card.export(body.rule_ids, actor=user.user_id)
    except proof_card.ProofKeyUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    stamp = card["exported_at"][:19].replace(":", "").replace("-", "")
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="vigil-proof-card_{card["kind"]}_{stamp}.zip"'
        },
    )


@router.get("/proof-card/key")
def get_proof_card_key(user: User = Depends(get_current_active_user)):
    """The public key proof cards are signed with, and its id; made on first
    use, which the trail records as this user."""
    try:
        return proof_card.public_key(actor=user.user_id)
    except proof_card.ProofKeyUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/rules/{rule_id}/exceptions")
def get_rule_exceptions(rule_id: int):
    """An overlay copy's exceptions, active and removed, each with its per-day
    matches; ``handed_off`` when a SIEM copy runs the rule, so Vigil applies none."""
    try:
        return exceptions.of_copy(rule_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/rules/{rule_id}/exceptions", dependencies=_SETTINGS_WRITE)
def add_rule_exception(
    rule_id: int,
    request: AddExceptionRequest,
    current_user: User = Depends(get_current_active_user),
):
    """Add an exception to an overlay copy, attributed to the signed-in user."""
    with acting_as(current_user.username):
        return _edited(
            exceptions.edit_exception(
                "add", rule_id=rule_id, match=request.match, note=request.note
            )
        )


@router.delete(
    "/rules/{rule_id}/exceptions/{exception_id}", dependencies=_SETTINGS_WRITE
)
def remove_rule_exception(
    rule_id: int,
    exception_id: int,
    reason: str = Query(max_length=2000),
    current_user: User = Depends(get_current_active_user),
):
    """Remove (soft) one of an overlay copy's exceptions, with a reason."""
    if exceptions.rule_of(exception_id) != rule_id:
        raise HTTPException(
            status_code=404,
            detail=f"No exception {exception_id} on rule {rule_id}",
        )
    with acting_as(current_user.username):
        return _edited(
            exceptions.edit_exception(
                "remove", exception_id=exception_id, reason=reason
            )
        )


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
