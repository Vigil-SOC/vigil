"""The MCP runner, seen from the backend and the daemon (#2303).

With ``MCP_RUNNER_URL`` set, every catalog server runs in the runner
(``services/mcp_runner``): ``PUT /servers/{name}`` starts it with the env this
process resolved, MCP streamable HTTP at ``/servers/{name}/mcp`` reaches it, and
``DELETE`` stops it. Unset, each server is a stdio child (start.sh, tests)
spawned with the same ``runner_env``, but as this process's own user.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable, Dict, Optional

from core.integrations.integration_bridge_service import FALCON_ENV, base_of

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

    # An instance (#5353) runs where its base does unless named itself.
    override = next(
        (
            value
            for name in (server_name, base_of(server_name))
            if name
            and (
                value := os.environ.get(  # noqa: ENV001 - per-server names
                    "MCP_RUNNER_URL_" + name.upper().replace("-", "_")
                )
            )
        ),
        None,
    )
    url = override or get_settings().mcp_runner_url
    return url.rstrip("/") or None


def _env_value(value) -> str:
    # Lower-case booleans: env readers compare against "true", not "True".
    return str(value).lower() if isinstance(value, bool) else str(value)


# falcon-mcp reads these. env_var_for stays CROWDSTRIKE_* so Helm, Compose and
# env.example still resolve; a saved client id and base URL are copied here.
# An empty base URL leaves the catalog's https://api.crowdstrike.com literal.
_FALCON_ENV = FALCON_ENV


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
    base = getattr(server, "instance_of", None)
    if base:
        _instance_env(server.name, base, env)
        return env
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
        if descriptor.id == "crowdstrike":
            for field, target in _FALCON_ENV.items():
                value = fields.get(field)
                if value is not None and value != "":
                    env[target] = _env_value(value)
    return env


def _instance_env(name: str, base: str, env: Dict[str, str]) -> None:
    """An instance's settings (#5353): under its own names, and again under
    every name its base's launch reads, in this process's env only."""
    from core.integrations._base.config import resolve
    from core.integrations._base.descriptor import get_descriptor
    from core.integrations.integration_bridge_service import (
        instance_keys,
        launch_fields,
    )

    env["VIGIL_INTEGRATION_ID"] = name
    descriptor = get_descriptor(base)
    try:
        fields = resolve(descriptor, name) if descriptor else {}
    except Exception as exc:  # noqa: BLE001 - one integration must not block the start
        logger.warning("Could not resolve %s: %s", name, exc)
        return
    own = instance_keys(name)
    for var, field in [*((v, f) for f, v in own.items()), *launch_fields(base).items()]:
        value = fields.get(field)
        if value is not None and value != "":
            env[var] = _env_value(value)


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
        of: Optional[str] = None,
    ):
        self.url, self.name = url, name
        # An instance's base entry: the runner launches that, under its uid.
        self.of = of
        self._env, self._enabled = env, enabled
        # What the last PUT did: started, restarted, unchanged.
        self.last_action: Optional[str] = None

    def enabled(self) -> bool:
        return self._enabled()

    async def start(self) -> str:
        """PUT the server with its env: starts it, restarts on new env, else no-op."""
        import asyncio

        env = await asyncio.to_thread(self._env)
        body: Dict[str, object] = {"env": env}
        if self.of:
            body["of"] = self.of
        async with _http() as http:
            response = await http.put(f"{self.url}/servers/{self.name}", json=body)
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
