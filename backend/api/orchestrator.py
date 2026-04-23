"""Orchestrator API endpoints for autonomous investigation management.

Provides REST endpoints to control the orchestrator (enable/disable/kill),
view investigations, read working directory files, and trigger manual
investigations.
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


_cached_orchestrator = None


def _get_orchestrator():
    """Get a cached orchestrator instance for DB-only queries from the API layer."""
    global _cached_orchestrator
    if _cached_orchestrator is not None:
        return _cached_orchestrator
    try:
        from daemon.orchestrator import Orchestrator
        from daemon.config import OrchestratorConfig

        config = OrchestratorConfig()
        orch = Orchestrator(config)
        orch._init_services()
        _cached_orchestrator = orch
        return orch
    except Exception as e:
        logger.debug(f"Orchestrator init: {e}")
        return None


class InvestigationCreateRequest(BaseModel):
    workflow_id: str = "incident-response"
    finding_ids: list = []
    case_id: Optional[str] = None
    hypothesis: Optional[str] = None
    priority: str = "medium"


class OrchestratorToggleRequest(BaseModel):
    enabled: bool


# ---- Status & Control ----

@router.get("/status")
async def get_orchestrator_status():
    """Get orchestrator status: enabled state, active agents, stats, cost."""
    try:
        orch = _get_orchestrator()
        
        investigations = []
        cost_summary = {}
        stats = {}
        
        if orch:
            investigations = orch.get_all_investigations()
            cost_summary = orch.get_cost_summary()
            stats = orch.stats

        # Enabled is persisted inside the single `orchestrator.settings` key.
        # See services/config_service and backend/api/config.py.
        enabled = False
        try:
            from database.config_service import get_config_service
            settings = get_config_service().get_system_config('orchestrator.settings')
            if isinstance(settings, dict):
                enabled = bool(settings.get("enabled", False))
        except Exception:
            enabled = orch.enabled if orch else False
        
        active = [i for i in investigations if i.get("status") in ("assigned", "executing")]
        queued = [i for i in investigations if i.get("status") == "queued"]
        completed = [i for i in investigations if i.get("status") == "completed"]
        failed = [i for i in investigations if i.get("status") == "failed"]
        review = [i for i in investigations if i.get("status") == "review_submitted"]
        
        max_agents = 3
        try:
            from database.config_service import get_config_service
            orch_cfg = get_config_service().get_system_config('orchestrator.settings')
            if orch_cfg and isinstance(orch_cfg, dict):
                max_agents = int(orch_cfg.get('max_concurrent_agents', 3))
        except Exception:
            if orch:
                max_agents = orch.config.max_concurrent_agents

        return {
            "enabled": enabled,
            "active_agents": len(active),
            "max_concurrent_agents": max_agents,
            "queued": len(queued),
            "completed": len(completed),
            "failed": len(failed),
            "pending_review": len(review),
            "total_investigations": len(investigations),
            "cost": cost_summary,
            "stats": stats,
        }
    except Exception as e:
        logger.error(f"Error getting orchestrator status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _persist_orchestrator_enabled(enabled: bool) -> None:
    """Write the `enabled` flag into the single `orchestrator.settings` key.

    Read-modify-write so the rest of the settings struct is preserved. If no
    settings row exists yet (first toggle on a fresh DB), seed it from the
    defaults defined in backend/api/config.py.
    """
    try:
        from database.config_service import get_config_service
        from backend.api.config import ORCHESTRATOR_DEFAULTS

        svc = get_config_service(user_id='web_ui')
        current = svc.get_system_config('orchestrator.settings')
        base = dict(current) if isinstance(current, dict) else dict(ORCHESTRATOR_DEFAULTS)
        base["enabled"] = bool(enabled)
        svc.set_system_config(
            key='orchestrator.settings',
            value=base,
            description='Autonomous orchestrator settings',
            config_type='orchestrator',
            change_reason=f'Orchestrator {"enabled" if enabled else "disabled"} via API',
        )
    except Exception as e:
        logger.warning("Could not persist orchestrator.settings.enabled: %s", e)


@router.post("/enable")
async def enable_orchestrator():
    """Enable the orchestrator at runtime."""
    try:
        orch = _get_orchestrator()
        if orch:
            orch.enable()
        _persist_orchestrator_enabled(True)
        return {"success": True, "enabled": True, "message": "Orchestrator enabled"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/disable")
async def disable_orchestrator():
    """Gracefully disable the orchestrator. Running agents finish their current step."""
    try:
        orch = _get_orchestrator()
        if orch:
            orch.disable()
        _persist_orchestrator_enabled(False)
        return {"success": True, "enabled": False, "message": "Orchestrator disabled (graceful)"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/kill")
async def kill_orchestrator():
    """Emergency kill: cancel all running agents immediately."""
    try:
        orch = _get_orchestrator()
        if orch:
            await orch.kill()
        return {"success": True, "message": "All agents killed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---- Investigations ----

@router.get("/investigations")
async def list_investigations(status: Optional[str] = Query(None)):
    """List all investigations with optional status filter."""
    try:
        orch = _get_orchestrator()
        if not orch:
            return {"investigations": [], "count": 0}
        
        investigations = orch.get_all_investigations(status=status)
        return {
            "investigations": investigations,
            "count": len(investigations),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/investigations/{investigation_id}")
async def get_investigation(investigation_id: str):
    """Get detailed information about a specific investigation."""
    try:
        orch = _get_orchestrator()
        if not orch:
            raise HTTPException(status_code=404, detail="Orchestrator not available")
        
        inv = orch.get_investigation(investigation_id)
        if not inv:
            raise HTTPException(status_code=404, detail=f"Investigation not found: {investigation_id}")
        
        files = orch.workdir.list_files(investigation_id)
        log = orch.workdir.get_log(investigation_id, tail=20)
        state = orch.workdir.read_state(investigation_id)
        disk_usage = orch.workdir.get_disk_usage(investigation_id)
        
        return {
            **inv,
            "files": files,
            "recent_log": log,
            "state": state,
            "disk_usage_bytes": disk_usage,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/investigations/{investigation_id}/files/{filename:path}")
async def get_investigation_file(investigation_id: str, filename: str):
    """Read a file from an investigation's working directory."""
    try:
        orch = _get_orchestrator()
        if not orch:
            raise HTTPException(status_code=404, detail="Orchestrator not available")
        
        content = orch.workdir.read_file(investigation_id, filename)
        if not content and not orch.workdir.exists(investigation_id):
            raise HTTPException(status_code=404, detail=f"Investigation not found: {investigation_id}")
        
        is_json = filename.endswith(".json") or filename.endswith(".jsonl")
        
        return {
            "filename": filename,
            "investigation_id": investigation_id,
            "content": content,
            "content_type": "application/json" if is_json else "text/plain",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/investigations/{investigation_id}/wake")
async def wake_investigation(investigation_id: str):
    """Wake a sleeping investigation for further work."""
    try:
        orch = _get_orchestrator()
        if not orch:
            raise HTTPException(status_code=404, detail="Orchestrator not available")
        
        inv = orch.get_investigation(investigation_id)
        if not inv:
            raise HTTPException(status_code=404, detail=f"Investigation not found: {investigation_id}")
        
        if inv.get("status") not in ("sleeping", "needs_rework", "completed", "failed"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot wake investigation in status '{inv.get('status')}'"
            )
        
        state = orch.workdir.read_state(investigation_id)
        state["status"] = "executing"
        state.pop("failure_reason", None)
        state["error_count"] = 0
        orch.workdir.write_state(investigation_id, state)
        orch._update_investigation_status(investigation_id, "assigned")
        
        return {"success": True, "message": f"Investigation {investigation_id} woken up"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/investigations/{investigation_id}/kill")
async def kill_investigation(investigation_id: str):
    """Kill a specific running investigation."""
    try:
        orch = _get_orchestrator()
        if not orch:
            raise HTTPException(status_code=404, detail="Orchestrator not available")
        
        await orch.agent_runner.stop_agent(investigation_id)
        orch._update_investigation_status(investigation_id, "failed", "Manually killed by user")
        
        state = orch.workdir.read_state(investigation_id)
        state["status"] = "failed"
        state["failure_reason"] = "Manually killed by user"
        orch.workdir.write_state(investigation_id, state)
        
        return {"success": True, "message": f"Investigation {investigation_id} killed"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ReviewRequest(BaseModel):
    action: str  # 'approve' or 'rework'
    notes: Optional[str] = None


@router.post("/investigations/{investigation_id}/review")
async def review_investigation(investigation_id: str, request: ReviewRequest):
    """Human review of an investigation: approve or request rework."""
    if request.action not in ("approve", "rework"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'rework'")

    try:
        orch = _get_orchestrator()
        if not orch:
            raise HTTPException(status_code=503, detail="Orchestrator not available")

        inv = orch.get_investigation(investigation_id)
        if not inv:
            raise HTTPException(status_code=404, detail=f"Investigation not found: {investigation_id}")

        if inv.get("status") != "review_submitted":
            raise HTTPException(
                status_code=400,
                detail=f"Investigation is in '{inv.get('status')}' status, not 'review_submitted'",
            )

        new_status = "completed" if request.action == "approve" else "needs_rework"
        orch._update_investigation_status(investigation_id, new_status, request.notes)

        from database.connection import get_db_manager
        from database.models import Investigation as InvestigationModel

        with get_db_manager().session_scope() as session:
            db_inv = session.query(InvestigationModel).filter_by(investigation_id=investigation_id).first()
            if db_inv:
                db_inv.master_review_notes = request.notes or (
                    "Approved by human reviewer" if request.action == "approve" else "Rework requested by human reviewer"
                )

        if request.action == "rework":
            state = orch.workdir.read_state(investigation_id)
            state["status"] = "executing"
            state.pop("failure_reason", None)
            orch.workdir.write_state(investigation_id, state)
            orch._update_investigation_status(investigation_id, "assigned")

        return {
            "success": True,
            "action": request.action,
            "new_status": new_status if request.action == "approve" else "assigned",
            "message": f"Investigation {request.action}d",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reviewing investigation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/investigations")
async def create_investigation(request: InvestigationCreateRequest):
    """Manually create a new investigation."""
    try:
        orch = _get_orchestrator()
        if not orch:
            raise HTTPException(status_code=503, detail="Orchestrator not available")
        
        orch.investigation_queue.put_nowait({
            "type": "manual",
            "workflow_id": request.workflow_id,
            "finding_ids": request.finding_ids,
            "case_id": request.case_id,
            "hypothesis": request.hypothesis,
            "priority": request.priority,
        })
        
        return {
            "success": True,
            "message": "Investigation queued for creation",
            "workflow_id": request.workflow_id,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---- Scan Existing Findings ----

class ScanFindingsRequest(BaseModel):
    severities: list = ["critical", "high"]


@router.post("/scan-findings")
async def scan_existing_findings(request: ScanFindingsRequest):
    """Scan existing findings in the DB and create investigations for all
    matching ones that haven't been investigated yet.

    Concurrency is controlled by the orchestrator's max_concurrent_agents
    setting -- investigations are queued and picked up as agent slots open.
    """
    try:
        from database.connection import get_db_manager
        from database.models import Finding, Investigation

        skipped_existing = 0

        with get_db_manager().session_scope() as session:
            already_investigated = set()
            for inv in session.query(Investigation).all():
                for tid in (inv.trigger_ids or []):
                    already_investigated.add(tid)

            findings = (
                session.query(Finding)
                .filter(Finding.severity.in_(request.severities))
                .order_by(Finding.timestamp.desc())
                .all()
            )

            to_investigate = []
            for f in findings:
                fid = f.finding_id
                if fid in already_investigated:
                    skipped_existing += 1
                    continue
                to_investigate.append({
                    "finding_id": fid,
                    "severity": f.severity,
                    "title": f.description[:200] if f.description else "",
                    "data_source": f.data_source,
                })

        orch = _get_orchestrator()
        created = 0
        if orch and to_investigate:
            for finding_data in to_investigate:
                try:
                    await orch._create_investigation(
                        workflow_id="incident-response",
                        findings=[finding_data],
                        trigger_type="scan",
                        priority=finding_data.get("severity", "medium"),
                        shutdown_event=None,
                    )
                    created += 1
                except Exception as e:
                    logger.warning(f"Failed to create investigation for {finding_data.get('finding_id')}: {e}")

        return {
            "success": True,
            "created": created,
            "skipped_already_investigated": skipped_existing,
            "total_matching": created + skipped_existing,
            "message": f"Queued {created} investigations (will run up to max_concurrent_agents at a time)"
                       + (f", {skipped_existing} already investigated" if skipped_existing else ""),
        }
    except Exception as e:
        logger.error(f"Error scanning findings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---- Cost ----

@router.get("/cost")
async def get_cost_summary():
    """Get cost breakdown across all investigations."""
    try:
        orch = _get_orchestrator()
        if not orch:
            return {"total_cost_usd": 0, "active_cost_usd": 0}
        return orch.get_cost_summary()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
