"""MCP runner control API and streamable-HTTP bridge, on port 6991.

    PUT    /servers/{name}      {"env": {...}}  start, restart on changed env, else no-op
    DELETE /servers/{name}                      stop
    GET    /servers                             status of every catalog server
    *      /servers/{name}/mcp                  MCP streamable HTTP to the running child

Every route takes ``Authorization: Bearer $MCP_RUNNER_TOKEN``; with the token
empty every route answers 503.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from core.integrations.mcp.runner import runner_token
from services.mcp_runner.catalog import Catalog, load_uids
from services.mcp_runner.supervisor import ShuttingDown, Supervisor

logger = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path("/var/lib/vigil/mcp")
PORT = 6991


# glibc's own unsecure-envvars list (what it strips for setuid programs), plus
# PATH, which picks the binaries setpriv and the server run.
_ROOT_UNSAFE_VARS = {
    "GCONV_PATH",
    "GETCONF_DIR",
    "GLIBC_TUNABLES",
    "HOSTALIASES",
    "LOCALDOMAIN",
    "LOCPATH",
    "MALLOC_TRACE",
    "NIS_PATH",
    "NLSPATH",
    "PATH",
    "RESOLV_HOST_CONF",
    "RES_OPTIONS",
    "TMPDIR",
    "TZDIR",
}


def _is_root_unsafe(key: str) -> bool:
    return key.startswith("LD_") or key in _ROOT_UNSAFE_VARS


class BearerAuth:
    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            return  # no websocket routes; refuse rather than pass through
        if not self.token:
            response = JSONResponse({"detail": "MCP_RUNNER_TOKEN is not set"}, 503)
            return await response(scope, receive, send)
        given = dict(scope["headers"]).get(b"authorization", b"")
        if not hmac.compare_digest(given, self.expected):
            return await JSONResponse({"detail": "unauthorized"}, 401)(
                scope, receive, send
            )
        await self.app(scope, receive, send)


def create_app(
    token: str,
    catalog: Catalog,
    state_dir: Path = STATE_DIR,
    switch_uid: Optional[bool] = None,
) -> ASGIApp:
    if switch_uid is None:
        switch_uid = os.geteuid() == 0
    supervisor = Supervisor(catalog, state_dir, load_uids(state_dir), switch_uid)

    def _unknown(name: str) -> Optional[JSONResponse]:
        if name not in catalog:
            return JSONResponse({"detail": f"{name} is not in the catalog"}, 404)
        return None

    async def list_servers(request: Request) -> JSONResponse:
        return JSONResponse(supervisor.status())

    async def put_server(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if (missing := _unknown(name)) is not None:
            return missing
        try:
            body = await request.json()
        except ValueError:
            body = {}  # no body: start with no env
        # Only env is read; command, args and cwd are the catalog's.
        env = body.get("env", {}) if isinstance(body, dict) else None
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            return JSONResponse({"detail": 'body must be {"env": {str: str}}'}, 422)
        # setpriv sees this env while still root; these would run code there
        # before it drops to the server's uid.
        if unsafe := sorted(k for k in env if _is_root_unsafe(k)):
            return JSONResponse({"detail": f"env may not set {unsafe}"}, 422)
        try:
            action, child = await supervisor.put(name, env)
        except ShuttingDown:
            return JSONResponse({"detail": "the runner is shutting down"}, 503)
        status = child.status()
        return JSONResponse(
            {"action": action, "running": child.state == "running", **status},
            200 if child.state == "running" else 502,
        )

    async def delete_server(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if (missing := _unknown(name)) is not None:
            return missing
        await supervisor.delete(name)
        return JSONResponse({"running": False, "state": "stopped"})

    async def mcp_endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        name = scope["path_params"]["name"]
        if (missing := _unknown(name)) is not None:
            return await missing(scope, receive, send)
        child = supervisor.running(name)
        if child is None or child.manager is None:
            response = JSONResponse({"detail": f"{name} is not running"}, 503)
            return await response(scope, receive, send)
        started = False

        async def tracked_send(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await child.manager.handle_request(scope, receive, tracked_send)
        except (RuntimeError, AttributeError, AssertionError):
            # The manager stopped (DELETE, restart) while this request was in
            # its body read or waiting to open a session; answer as for a
            # stopped server if we still can.
            if started:
                raise
            response = JSONResponse({"detail": f"{name} is not running"}, 503)
            await response(scope, receive, send)

    @asynccontextmanager
    async def lifespan(app):
        async with supervisor:
            yield

    app = Starlette(
        routes=[
            Route("/servers", list_servers, methods=["GET"]),
            Route("/servers/{name}", put_server, methods=["PUT"]),
            Route("/servers/{name}", delete_server, methods=["DELETE"]),
            Route("/servers/{name}/mcp", _AsgiEndpoint(mcp_endpoint)),
        ],
        lifespan=lifespan,
    )
    app.state.supervisor = supervisor
    return BearerAuth(app, token)


class _AsgiEndpoint:
    """Makes Starlette's Route hand the raw ASGI call through (any method)."""

    def __init__(self, handler):
        self.handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.handler(scope, receive, send)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # The runner's own bearer; it never reaches a child.
    token = runner_token()
    if not token:
        logger.error("MCP_RUNNER_TOKEN is empty: every route answers 503")
    app = create_app(token, Catalog(APP_ROOT))
    # Reached by service name over mcp-network, so it binds every interface;
    # the bearer gates it and compose publishes no port.
    uvicorn.run(
        app,
        host="0.0.0.0",  # nosec B104 - containers must bind all interfaces
        port=PORT,
        log_level="info",
        # A client's MCP GET stream never ends on its own, and uvicorn waits
        # for open streams before lifespan shutdown stops the servers.
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
