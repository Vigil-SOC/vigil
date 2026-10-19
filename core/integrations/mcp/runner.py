"""The MCP runner, seen from the backend and the daemon (#2303).

With ``MCP_RUNNER_URL`` set, every catalog server runs in the runner
(``services/mcp_runner``): ``PUT /servers/{name}`` starts it with the env this
process resolved, MCP streamable HTTP at ``/servers/{name}/mcp`` reaches it, and
``DELETE`` stops it. Unset, each server is a stdio child (start.sh, tests).
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, Optional

logger = logging.getLogger(__name__)


def runner_token() -> str:
    """The runner's bearer: ``MCP_RUNNER_TOKEN``, else ``MCP_RUNNER_TOKEN_FILE``.

    Read from the environment by the runner and its clients alike; the runner
    has no secret store to read it from.
    """
    token = os.environ.get("MCP_RUNNER_TOKEN", "")  # noqa: ENV001 - runner bearer
    path = os.environ.get("MCP_RUNNER_TOKEN_FILE", "")  # noqa: ENV001 - same
    if not token and path:
        try:
            token = Path(path).read_text().strip()
        except OSError as exc:
            logger.error("Could not read MCP_RUNNER_TOKEN_FILE %s: %s", path, exc)
    return token


def runner_url(server_name: str) -> Optional[str]:
    """The runner serving ``server_name``, or None to run it as a stdio child."""
    from core.config import get_settings

    key = "MCP_RUNNER_URL_" + server_name.upper().replace("-", "_")
    override = os.environ.get(key)  # noqa: ENV001 - per-server names, built here
    url = override or get_settings().mcp_runner_url
    return url.rstrip("/") or None


def _env_value(value) -> str:
    # Lower-case booleans: env readers compare against "true", not "True".
    return str(value).lower() if isinstance(value, bool) else str(value)


def runner_env(server) -> Dict[str, str]:
    """The env block the runner starts ``server`` with, resolved here.

    The catalog's declared env as substituted for this process, every
    ``${VAR}`` credential it names, and each integration field that backs the
    server under its ``env_var_for`` name, which ``resolve()`` falls back to
    when the server has no database or secret store, as in the runner.
    Never command, args or cwd: the runner launches from its own catalog.
    """
    from core.integrations._base.config import resolve
    from core.integrations._base.descriptor import iter_descriptors
    from core.integrations.integration_secrets import env_var_for
    from core.secrets import get_secret

    env: Dict[str, str] = {}
    for key in server.declared_env:
        if value := (server.env or {}).get(key):
            env[key] = value
    for var in server.required_env_vars:
        if value := get_secret(var):
            env[var] = value
    for descriptor in iter_descriptors():
        if server.name not in descriptor.mcp_server_names:
            continue
        try:
            fields = resolve(descriptor)
        except (
            Exception
        ) as exc:  # noqa: BLE001 - one integration must not block the start
            logger.warning(
                "Could not resolve %s for %s: %s", descriptor.id, server.name, exc
            )
            continue
        for field, value in fields.items():
            if value is not None and value != "":
                env[env_var_for(descriptor.id, field)] = _env_value(value)
    return env


class RunnerError(RuntimeError):
    """The runner refused or failed a request; the message is its ``detail``."""


def _http():
    from mcp.shared._httpx_utils import create_mcp_http_client

    return create_mcp_http_client(headers={"Authorization": f"Bearer {runner_token()}"})


def _detail(response) -> str:
    try:
        detail = response.json().get("detail")
    except Exception:  # noqa: BLE001 - not JSON
        detail = None
    if not detail and response.status_code == 502:
        detail = "the server did not start"
    return f"MCP runner {response.status_code}: {detail or response.text[:200]}"


class RunnerServer:
    """One catalog server in the runner, as one client process uses it.

    The API's workers and the daemon share the runner's one copy of each
    server, so only an enable/disable or restart stops it (``stop``); a client
    closing its own session leaves it running for the others.
    """

    def __init__(
        self,
        url: str,
        name: str,
        env: Callable[[], Dict[str, str]],
        enabled: Callable[[], bool],
    ):
        self.url, self.name = url, name
        self._env, self._enabled = env, enabled
        # What the last PUT did: started, restarted, unchanged.
        self.last_action: Optional[str] = None

    def enabled(self) -> bool:
        return self._enabled()

    async def start(self) -> str:
        """PUT the server with its env: starts it, restarts on new env, else no-op."""
        import asyncio

        env = await asyncio.to_thread(self._env)
        async with _http() as http:
            response = await http.put(
                f"{self.url}/servers/{self.name}", json={"env": env}
            )
        if response.status_code != 200:
            raise RunnerError(_detail(response))
        self.last_action = response.json().get("action")
        return self.last_action or ""

    async def stop(self) -> None:
        async with _http() as http:
            response = await http.delete(f"{self.url}/servers/{self.name}")
        if response.status_code != 200:
            raise RunnerError(_detail(response))

    @asynccontextmanager
    async def streams(self) -> AsyncIterator[tuple]:
        from mcp.client.streamable_http import streamable_http_client

        async with _http() as http, streamable_http_client(
            f"{self.url}/servers/{self.name}/mcp", http_client=http
        ) as (read, write):
            yield read, write
