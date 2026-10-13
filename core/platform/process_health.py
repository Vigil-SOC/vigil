"""Whether the agent layer and the SOC daemon are up, as the API sees them.

The console is served without either, so a fresh install can look ready while
every chat turn and workflow run fails (no agent layer, or no shared token) and
no alert is polled or triaged (no daemon). GET /api/health reports both, for the
setup checks and the status line. Each read is the route that process's own
probes already use; nothing here adds a signal.

Neither feeds the health ``status``: a stopped daemon is the foreground default
(#902), and failing readiness on another process would eject every API pod.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

import httpx

from core.config import get_settings
from core.secrets import get_secret

logger = logging.getLogger(__name__)

# The public health route, and so every probe, waits on this, so it is short and
# both reads run at once. A hung process costs a probe one second, not a restart.
READ_TIMEOUT_S = 1.0


async def _status_code(client: httpx.AsyncClient, url: str) -> Optional[int]:
    """The answer's status code. None when nothing answered: refused,
    unresolvable and too slow all mean not running."""
    try:
        response = await asyncio.wait_for(client.get(url), READ_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        logger.debug("process health read of %s failed: %s", url, exc)
        return None
    return response.status_code


async def _agent_layer(client: httpx.AsyncClient) -> str:
    # The backend's half of the seam. Without it every /internal call answers
    # 503, so an agent layer that is up still can't run anything.
    try:
        if not get_secret("AGENT_INTERNAL_TOKEN"):
            return "no_token"
    except Exception as exc:  # noqa: BLE001 — a status read must not fail the route
        logger.debug("could not read AGENT_INTERNAL_TOKEN: %s", exc)
    # agent-serve, at the address chat already uses. The worker's own port is
    # probe-only under the chart's NetworkPolicy, and every install shape starts
    # the two together.
    url = f"{get_settings().agent_url.rstrip('/')}/readyz"
    code = await _status_code(client, url)
    if code is None:
        return "not_running"
    return "running" if code == 200 else "unhealthy"


async def _daemon(client: httpx.AsyncClient) -> str:
    # 503 means a component task died (services/daemon/metrics.py).
    settings = get_settings()
    url = f"http://{settings.daemon_health_host}:{settings.daemon_health_port}/health"
    code = await _status_code(client, url)
    if code is None:
        return "not_running"
    return "running" if code == 200 else "unhealthy"


async def read_process_health() -> Dict[str, str]:
    """``agent_layer`` and ``daemon``: running, unhealthy or not_running, and
    no_token for the agent layer. Words only: the health route is public."""
    async with httpx.AsyncClient(timeout=READ_TIMEOUT_S) as client:
        agent_layer, daemon = await asyncio.gather(
            _agent_layer(client), _daemon(client)
        )
    return {"agent_layer": agent_layer, "daemon": daemon}
