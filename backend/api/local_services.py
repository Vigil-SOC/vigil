"""Local Services API — start/stop/status for Docker services and Ollama.

All orchestration lives in ``services/service_manager.py``; this module is the
HTTP surface over it. Two structural rules:

- Handlers are **sync** ``def``. They call blocking subprocesses, so an
  ``async def`` would stall the event loop — and every request in the process —
  for the duration of a compose command. FastAPI runs sync defs in a threadpool.
- The literal ``/splunk/*`` and ``/postgres/status`` routes are registered
  **before** the generic ``/{name}/*`` routes. FastAPI matches in registration
  order, so the reverse would let ``/{name}/status`` swallow ``/splunk/status``.
"""

import logging
from typing import Callable, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.middleware.auth import get_current_active_user, require_settings_admin
from database.models import User
from services import service_manager
from services.autostart_config import get_autostart_services, set_autostart_services
from services.service_manager import SERVICES, ActionResult, ServiceStatus

router = APIRouter()
logger = logging.getLogger(__name__)

# Path params are constrained to the registry, so an argument like `-f/etc/passwd`
# is rejected by FastAPI before any of our code runs.
ServiceName = str


class AutostartRequest(BaseModel):
    services: List[str]


def _resolve(name: str):
    if name not in SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {name}")


def _status_payload(s: ServiceStatus) -> dict:
    return {
        "name": s.name,
        "kind": s.kind,
        "running": s.running,
        "ready": s.ready,
        "installed": s.installed,
        "status": s.status,
        "managed_by_vigil": s.managed_by_vigil,
        "startable": s.startable,
        "stoppable": s.stoppable,
        "required": s.required,
        "description": s.description,
        "detail": s.detail,
    }


def _action_payload(r: ActionResult) -> dict:
    return {
        "success": r.success,
        "message": r.message,
        "already_running": r.already_running,
        "code": r.code,
        **r.detail,
    }


# --- Legacy routes (registered first; shapes frozen for the existing client) ---


@router.get("/splunk/status")
def get_splunk_status():
    s = service_manager.status("splunk")
    return {
        "installed": True,
        "running": s.running,
        "status": s.status,
        "container_name": "deeptempo-splunk",
        "web_url": "http://localhost:6990" if s.running else None,
        "hec_url": "http://localhost:8088" if s.running else None,
        "username": "admin",
        "note": "Default password: changeme123",
    }


@router.post("/splunk/start")
def start_splunk(current_user: User = Depends(get_current_active_user)):
    require_settings_admin(current_user)
    r = service_manager.start("splunk")
    if not r.success:
        raise HTTPException(status_code=500, detail=r.message)
    if r.already_running:
        return {
            "success": True,
            "message": "Splunk is already running",
            "web_url": "http://localhost:6990",
            "already_running": True,
        }
    return {
        "success": True,
        "message": "Splunk is starting. It may take 2-3 minutes to be fully ready.",
        "web_url": "http://localhost:6990",
        "hec_url": "http://localhost:8088",
        "username": "admin",
        "password": "changeme123",
        "note": "First startup may take several minutes. "
        "Check status endpoint for ready state.",
    }


@router.post("/splunk/stop")
def stop_splunk(current_user: User = Depends(get_current_active_user)):
    require_settings_admin(current_user)
    r = service_manager.stop("splunk")
    if not r.success:
        raise HTTPException(status_code=500, detail=r.message)
    return {"success": True, "message": "Splunk stopped successfully"}


@router.post("/splunk/restart")
def restart_splunk(current_user: User = Depends(get_current_active_user)):
    require_settings_admin(current_user)
    r = service_manager.restart("splunk")
    if not r.success:
        raise HTTPException(status_code=500, detail=r.message)
    return {
        "success": True,
        "message": "Splunk restarted successfully",
        "web_url": "http://localhost:6990",
    }


@router.get("/postgres/status")
def get_postgres_status():
    s = service_manager.status("postgres")
    return {
        "installed": True,
        "running": s.running,
        "status": s.status,
        "container_name": "deeptempo-postgres",
        "host": "localhost",
        "port": 5432,
        "database": "deeptempo_soc",
    }


# --- Autostart (literal path; must precede /{name}) ---


@router.get("/autostart")
def read_autostart():
    return {"services": get_autostart_services(), "available": list(SERVICES)}


@router.put("/autostart")
def write_autostart(
    body: AutostartRequest, current_user: User = Depends(get_current_active_user)
):
    require_settings_admin(current_user)
    try:
        return {"services": set_autostart_services(body.services)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(
            status_code=500, detail=f"Could not persist autostart list: {e}"
        )


# --- Generic routes ---


@router.get("")
@router.get("/")
def list_services():
    ok, detail = service_manager.docker_available()
    return {
        "docker_available": ok,
        "docker_detail": detail,
        "autostart": get_autostart_services(),
        "services": [_status_payload(s) for s in service_manager.list_services()],
    }


@router.get("/{name}/status")
def service_status(name: ServiceName):
    _resolve(name)
    return _status_payload(service_manager.status(name))


@router.post("/{name}/start")
def service_start(
    name: ServiceName,
    wait: bool = Query(False),
    current_user: User = Depends(get_current_active_user),
):
    require_settings_admin(current_user)
    _resolve(name)
    r = service_manager.start(name, wait=wait)
    if not r.success:
        code = 409 if r.code in ("not_startable", "not_installed") else 500
        raise HTTPException(status_code=code, detail=r.message)
    return _action_payload(r)


def _lifecycle(name: str, current_user: User, action: Callable[[str], ActionResult]):
    """Shared body for the stop/restart routes: gate, resolve, act, translate."""
    require_settings_admin(current_user)
    _resolve(name)
    r = action(name)
    if not r.success:
        raise HTTPException(
            status_code=409 if r.code == "not_stoppable" else 500, detail=r.message
        )
    return _action_payload(r)


@router.post("/{name}/stop")
def service_stop(
    name: ServiceName, current_user: User = Depends(get_current_active_user)
):
    return _lifecycle(name, current_user, service_manager.stop)


@router.post("/{name}/restart")
def service_restart(
    name: ServiceName, current_user: User = Depends(get_current_active_user)
):
    return _lifecycle(name, current_user, service_manager.restart)
