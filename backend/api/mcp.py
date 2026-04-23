"""MCP Server management API endpoints."""

import logging
from typing import Dict, List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from services.mcp_service import MCPService

logger = logging.getLogger(__name__)
router = APIRouter()


def _service() -> MCPService:
    """Return the process-wide MCPService instance.

    Both the API endpoints and the MCPClient used to wrap in their own
    ``MCPService()`` — two instances, two cached ``_enabled_servers``
    dicts, so ``set_server_enabled`` on A was never visible to
    ``connect_to_server`` on B. Centralising through ``get_mcp_client()``
    ensures a single source of truth. Falls back to a local instance
    if the MCP SDK isn't installed so the introspection endpoints
    still work.
    """
    try:
        from services.mcp_client import get_mcp_client

        client = get_mcp_client()
        if client is not None:
            return client.mcp_service
    except Exception:
        pass
    if not hasattr(_service, "_fallback"):
        _service._fallback = MCPService()  # type: ignore[attr-defined]
    return _service._fallback  # type: ignore[attr-defined]


class _ServiceProxy:
    """Back-compat alias so existing ``mcp_service.X`` call sites keep working."""

    def __getattr__(self, name):
        return getattr(_service(), name)


mcp_service = _ServiceProxy()


class ServerControl(BaseModel):
    """Server control request."""
    action: str  # start or stop


class ServerEnabledRequest(BaseModel):
    """Request body for enabling/disabling a server."""
    enabled: bool


@router.get("/servers")
async def list_servers():
    """
    Get list of all MCP servers.
    
    Returns:
        List of server names
    """
    servers = mcp_service.list_servers()
    return {"servers": servers}


@router.get("/servers/status")
async def get_servers_status():
    """
    Get status of all MCP servers including enabled state.
    
    Returns:
        List of server status objects with enabled flag
    """
    statuses_dict = mcp_service.get_all_statuses()
    enabled_dict = mcp_service.get_all_enabled_states()
    # Convert dict to list of objects for frontend
    statuses_list = [
        {"name": name, "status": status, "enabled": enabled_dict.get(name, False)}
        for name, status in statuses_dict.items()
    ]
    return {"statuses": statuses_list}


@router.get("/servers/enabled")
async def get_enabled_states():
    """
    Get enabled/disabled state for all MCP servers.
    
    Returns:
        Dictionary of server_name -> enabled boolean
    """
    return {"enabled": mcp_service.get_all_enabled_states()}


@router.put("/servers/{server_name}/enabled")
async def set_server_enabled(server_name: str, request: ServerEnabledRequest):
    """Enable or disable an MCP server and apply the change at runtime.

    Transactional: persisting the enabled bit also triggers an actual
    connect (on enable) or disconnect (on disable), so the UI toggle
    becomes the single lever users need. Before this was wired, toggling
    only changed persisted state — the server didn't actually come online
    until the next backend restart.

    The response carries ``connected`` and ``error`` so the UI can flip
    the toggle back off and surface the real reason (e.g. missing creds,
    bad binary) when the connect attempt fails.
    """
    success = mcp_service.set_server_enabled(server_name, request.enabled)
    if not success:
        raise HTTPException(status_code=404, detail="Server not found")

    connected: Optional[bool] = None
    error: Optional[str] = None
    missing_credentials: Optional[List[str]] = None

    try:
        from services.mcp_client import get_mcp_client

        mcp_client = get_mcp_client()
    except Exception:
        mcp_client = None

    if request.enabled:
        # Try to bring the server online now. Failures (missing creds,
        # missing binary, unreachable remote MCP) surface via last_error.
        # Missing-credentials case is dormancy-by-design, not an error —
        # the UI treats it via the existing "Not Configured" chip.
        if mcp_client is not None:
            try:
                connected = await mcp_client.connect_to_server(
                    server_name, persistent=True
                )
                if not connected:
                    error = mcp_client.get_last_error(server_name)
                    missing_credentials = mcp_client.get_missing_credentials(
                        server_name
                    )
            except Exception as exc:  # noqa: BLE001
                connected = False
                error = f"{type(exc).__name__}: {exc}"
    else:
        # Disable → stop any running monitor process + tear down the
        # persistent MCP session so tools disappear from the pool.
        status = mcp_service.get_server_status(server_name)
        if status == "running":
            mcp_service.stop_server(server_name)
        if mcp_client is not None:
            try:
                await mcp_client.disconnect_from_server(server_name)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Disconnect for %s failed (non-fatal): %s", server_name, exc
                )

    return {
        "success": True,
        "server": server_name,
        "enabled": request.enabled,
        "connected": connected,
        "error": error,
        "missing_credentials": missing_credentials,
        "message": f"Server {server_name} {'enabled' if request.enabled else 'disabled'}",
    }


@router.get("/connections/status")
async def get_connections_status():
    """
    Get persistent connection status for all MCP servers.
    
    Returns:
        Connection status for each server
    """
    from services.mcp_client import get_mcp_client
    
    mcp_client = get_mcp_client()
    if not mcp_client:
        return {"error": "MCP client not available", "connections": {}}

    # Auto-heal: if a previously-dormant server's required env vars have
    # since resolved (user saved the credential via the integration
    # wizard, or set it in the shell), retry the connect here so the
    # next poll reflects the new state. Rate-limited per-server inside
    # retry_dormant_if_ready to avoid connect storms.
    try:
        await mcp_client.retry_dormant_if_ready()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Dormant-retry sweep skipped: %s", exc)

    status = mcp_client.get_connection_status()
    connections_list: List[Dict] = []
    for name, connected in status.items():
        entry: Dict = {"name": name, "connected": connected}
        if not connected:
            # Surface dormant-due-to-missing-creds so the UI can show the
            # "Not Configured" chip without a second roundtrip.
            missing = mcp_client.get_missing_credentials(name)
            if missing:
                entry["missing_credentials"] = missing
            err = mcp_client.get_last_error(name)
            if err:
                entry["error"] = err
        connections_list.append(entry)

    return {
        "connections": connections_list,
        "total": len(status),
        "connected": sum(1 for connected in status.values() if connected)
    }


@router.get("/servers/{server_name}/status")
async def get_server_status(server_name: str):
    """
    Get status of a specific server.
    
    Args:
        server_name: Name of the server
    
    Returns:
        Server status
    """
    status = mcp_service.get_server_status(server_name)
    if status is None:
        raise HTTPException(status_code=404, detail="Server not found")
    
    return {"server": server_name, "status": status}


# NOTE: the former POST /servers/{name}/start + /stop endpoints were
# removed when PUT /enabled became transactional. Every server in
# mcp-config.json is stdio-based, which the old `start_server` path
# explicitly refused (services/mcp_service.py), so those endpoints never
# worked for users. The enable toggle is now the single lever.


# NOTE: the former /servers/start-all + /servers/stop-all endpoints were
# removed alongside /start + /stop. They called the same stdio-hostile
# service methods and nothing in the UI invoked them.


@router.get("/servers/{server_name}/logs")
async def get_server_logs(server_name: str, lines: int = 100):
    """
    Get logs for a specific server.
    
    Args:
        server_name: Name of the server
        lines: Number of log lines to retrieve
    
    Returns:
        Server logs
    """
    logs = mcp_service.get_server_log(server_name, lines=lines)

    if logs == "":
        raise HTTPException(status_code=404, detail="Server not found")

    # Prepend the last connect-failure reason, if any — this is what
    # actually tells the user why a server isn't reachable. The log file
    # itself only exists for servers started via the monitor path.
    try:
        from services.mcp_client import get_mcp_client

        last_err = get_mcp_client().get_last_error(server_name)
        if last_err:
            logs = f"[last connect error] {last_err}\n\n{logs}"
    except Exception:
        pass

    return {"server": server_name, "logs": logs}


@router.get("/servers/{server_name}/test")
async def test_server(server_name: str):
    """
    Test if a server is responding.
    
    Args:
        server_name: Name of the server
    
    Returns:
        Test result
    """
    is_running = mcp_service.test_server(server_name)
    
    return {
        "server": server_name,
        "is_running": is_running,
        "status": "healthy" if is_running else "not responding"
    }


@router.post("/servers/reload")
async def reload_servers():
    """Reload MCP server configurations from ``mcp-config.json`` and
    the integration bridge, picking up newly enabled/disabled
    integrations without restarting the backend.

    Reinitialises the process-wide ``MCPService`` in place so both the
    API and the ``MCPClient`` see the new catalog. Previously-enabled
    servers are reconnected automatically; the old Popen-monitor path
    is gone (#125), so there's nothing here analogous to "restart
    running servers" — just enumerate new servers and let the enable
    toggle drive connects.
    """
    try:
        svc = _service()
        # Reinitialise servers dict in place so the MCPClient's reference
        # to this same instance keeps seeing the new catalog.
        svc.servers.clear()
        svc._initialize_servers()

        new_servers = list(svc.servers.keys())

        return {
            "success": True,
            "message": "MCP servers reloaded successfully",
            "total_servers": len(new_servers),
            "servers": new_servers,
        }
    
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reload MCP servers: {str(e)}"
        )

