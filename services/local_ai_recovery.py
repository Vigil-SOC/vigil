"""Local-only recovery for the Bifrost gateway used by Ollama enrichment.

This is intentionally unavailable outside a host-run development server. A
remote deployment must not allow an HTTP request to restart local containers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from services.runtime_config import get_ai_operations_setting

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_RECOVERY_LOCK = asyncio.Lock()
_HEALTH_WAIT_SECONDS = 45


@dataclass(frozen=True)
class RecoveryResult:
    """The result of one local gateway recovery attempt."""

    ready: bool
    restarted: bool
    detail: str


def _bifrost_url() -> str:
    return os.getenv("BIFROST_URL", "http://localhost:8080").rstrip("/")


def local_bifrost_recovery_enabled() -> bool:
    """Return true only for an explicitly local, host-run dev server."""
    if os.getenv("DEV_MODE", "false").lower() not in {"1", "true", "yes"}:
        return False
    if urlparse(_bifrost_url()).hostname not in _LOOPBACK_HOSTS:
        return False
    return bool(get_ai_operations_setting("local_ollama_recovery_enabled", True))


def local_bifrost_recovery_retry_limit() -> int:
    """Return the bounded number of retries following the initial failure."""
    value = get_ai_operations_setting("local_ollama_recovery_retry_limit", 1)
    try:
        return max(0, min(3, int(value)))
    except (TypeError, ValueError):
        return 1


def local_bifrost_restart_enabled() -> bool:
    return bool(get_ai_operations_setting("local_ollama_recovery_restart_gateway", True))


def is_gateway_connection_error(error: Exception) -> bool:
    """Recognize network errors without retrying provider or model errors."""
    if isinstance(
        error,
        (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError),
    ):
        return True
    return error.__class__.__name__ in {"APIConnectionError", "APIConnectionTimeout"}


async def _bifrost_healthy() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{_bifrost_url()}/health")
        return response.status_code == 200
    except httpx.HTTPError:
        return False


async def _restart_bifrost() -> bool:
    """Restart the known local Bifrost container without invoking a shell."""
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "restart",
            "deeptempo-bifrost",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except asyncio.TimeoutError:
        if process is not None:
            process.kill()
            await process.communicate()
        logger.warning("Local Bifrost restart timed out")
        return False
    except OSError as exc:
        logger.warning("Local Bifrost restart could not start: %s", exc)
        return False

    if process.returncode != 0:
        logger.warning("Local Bifrost restart failed: %s", stderr.decode().strip())
        return False
    return True


async def _wait_for_bifrost() -> bool:
    for _ in range(_HEALTH_WAIT_SECONDS):
        if await _bifrost_healthy():
            return True
        await asyncio.sleep(1)
    return False


async def recover_local_bifrost() -> RecoveryResult:
    """Make one serialized local recovery attempt, returning when Bifrost is ready."""
    if not local_bifrost_recovery_enabled():
        return RecoveryResult(False, False, "local recovery is disabled")

    async with _RECOVERY_LOCK:
        if await _bifrost_healthy():
            return RecoveryResult(True, False, "gateway is reachable; retrying the request")
        if not local_bifrost_restart_enabled():
            return RecoveryResult(False, False, "gateway is unavailable and automatic restart is disabled")

        logger.warning("Local Bifrost is unavailable; restarting its container")
        if not await _restart_bifrost():
            return RecoveryResult(False, True, "could not restart the local gateway")
        if await _wait_for_bifrost():
            return RecoveryResult(True, True, "local gateway restarted and is healthy")
        return RecoveryResult(False, True, "local gateway did not become healthy in time")
