"""Whether the agent layer and the SOC daemon are up, as the API sees them.

The console is served without either, so a fresh install can look ready while
every chat turn and workflow run fails (no agent layer, or no shared token) and
no alert is polled or triaged (no daemon). GET /api/processes reports both, for
the setup checks and the status line. Each read is the route that process's own
probes already use; nothing here adds a signal.

Signed-in only, and never part of /api/health: that route is public and probed,
and neither process decides whether this one is alive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal, Optional, Tuple, TypedDict, TypeVar

import httpx

from core.config import get_settings
from core.secrets import get_secret

logger = logging.getLogger(__name__)

# Per read. Both reads run at once, so a hung process costs a caller this much.
READ_TIMEOUT_S = 1.0
# Callers within this window share one answer, and at most one read is in
# flight: the status line polls from every open console.
CACHE_S = 5.0

DaemonState = Literal["running", "unhealthy", "not_running", "unknown"]
AgentLayerState = Literal[DaemonState, "no_token"]
_State = TypeVar("_State", AgentLayerState, DaemonState)


class ProcessStates(TypedDict):
    agent_layer: AgentLayerState
    daemon: DaemonState


class ProcessHealth:
    """Built once in the API's lifespan (``app.state.process_health``): one HTTP
    client, one cached answer and at most one read in flight for every caller."""

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        # No proxy from the environment: both addresses are this install's own,
        # and a proxy answering for a missing process would read as unhealthy.
        self._client = client or httpx.AsyncClient(
            timeout=READ_TIMEOUT_S, trust_env=False
        )
        self._answer: Optional[Tuple[float, ProcessStates]] = None
        self._reading: Optional[asyncio.Task[ProcessStates]] = None

    async def read(self) -> ProcessStates:
        """At most CACHE_S old."""
        if self._answer is not None and time.monotonic() - self._answer[0] < CACHE_S:
            return self._answer[1].copy()
        if self._reading is None or self._reading.done():
            self._reading = asyncio.ensure_future(self._read())
        # Shielded: one caller going away must not cancel the read the others
        # are waiting on. _read never raises.
        return (await asyncio.shield(self._reading)).copy()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _read(self) -> ProcessStates:
        # Each row on its own: a secrets error says nothing about the daemon.
        # A status read never fails its caller; an error is that row's unknown.
        agent_layer, daemon = await asyncio.gather(
            self._agent_layer(), self._daemon(), return_exceptions=True
        )
        states = ProcessStates(
            agent_layer=_or_unknown("agent_layer", agent_layer),
            daemon=_or_unknown("daemon", daemon),
        )
        self._answer = (time.monotonic(), states)
        return states

    async def _agent_layer(self) -> AgentLayerState:
        # The backend's half of the seam. Without it every /internal call answers
        # 503, so an agent layer that is up still can't run anything.
        if not get_secret("AGENT_INTERNAL_TOKEN"):
            return "no_token"
        # agent-serve, at the address chat already uses. The worker isn't read:
        # its port is probe-only under the chart's NetworkPolicy.
        return await self._state(f"{get_settings().agent_url.rstrip('/')}/readyz")

    async def _daemon(self) -> DaemonState:
        # A 503 here means a component task died (services/daemon/metrics.py).
        settings = get_settings()
        host, port = settings.daemon_health_host, settings.daemon_health_port
        return await self._state(f"http://{host}:{port}/health")

    async def _state(self, url: str) -> Literal["running", "unhealthy", "not_running"]:
        """running on a 200. not_running when refused or unresolvable. unhealthy
        for anything else (another status, too slow, not HTTP): something is
        there but not serving."""
        try:
            response = await asyncio.wait_for(self._client.get(url), READ_TIMEOUT_S)
        except httpx.ConnectError as exc:
            logger.debug("process health read of %s failed: %s", url, exc)
            return "not_running"
        except (asyncio.TimeoutError, httpx.TransportError) as exc:
            logger.debug("process health read of %s failed: %s", url, exc)
            return "unhealthy"
        return "running" if response.status_code == 200 else "unhealthy"


def _or_unknown(name: str, result: _State | BaseException) -> _State:
    if isinstance(result, BaseException):
        logger.error("Could not read the %s state", name, exc_info=result)
        return "unknown"
    return result
