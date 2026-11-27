"""Apply a rebuilt MCP catalog to this process's live sessions.

One diff-and-apply, :func:`apply_catalog`, serves ``POST /api/mcp/servers/reload``
and the re-merge of declared sources (:func:`run_declared_sources`) that each
process holding an MCP client runs: the API workers and the daemon. A source is
a catalog entry once merged (``MCPService.load_catalog``), so it is reconnected,
disconnected and withdrawn exactly as one.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from core.integrations.mcp.registry import MCPRegistry, deactivate, register_connected

logger = logging.getLogger(__name__)

# One apply at a time in a process: the route and the re-merge share svc.servers.
_apply_lock: Optional[asyncio.Lock] = None


def _lock() -> asyncio.Lock:
    global _apply_lock
    if _apply_lock is None:
        _apply_lock = asyncio.Lock()
    return _apply_lock


def spawn_changed(svc, old, new, session=None) -> bool:
    """Whether ``new`` would start differently from ``old``.

    Env counts only as mcp-config.json declares it (substituted), plus the
    credentials it names: the rest is resolved from saved integration settings
    at each connect (``runner_env``), stdio child or runner alike. A runner
    launches from its own catalog with only that env, so there command, args
    and cwd do not count. A remote entry changes when its URL or headers,
    resolved now, differ from what its live ``session`` was opened with.
    """
    from core.integrations.mcp.client import RemoteParams
    from core.integrations.mcp.runner import runner_url

    if new.url:
        params = getattr(session, "server_params", None)
        if not isinstance(params, RemoteParams):
            return True
        return (params.url, params.headers) != svc.remote_target(new)

    def spawn(server):
        env = server.env or {}
        declared = {k: env.get(k) for k in server.declared_env}
        if runner_url(server.name) is not None:
            return declared, server.required_env_vars
        return (
            declared,
            server.required_env_vars,
            server.command,
            server.args,
            server.cwd,
        )

    return spawn(old) != spawn(new)


def _withdraw(registry: Optional[MCPRegistry], name: str) -> None:
    if registry is not None:
        deactivate(registry, name)


async def apply_catalog(
    svc, mcp_client, registry: Optional[MCPRegistry], new_catalog: Dict[str, Any]
) -> Dict[str, Any]:
    """Swap ``new_catalog`` into ``svc.servers`` and bring live sessions to it.

    For each server with a live session: removed, or now disabled, it is
    disconnected (stopped in the runner too) and its tools withdrawn; a changed
    spawn config reconnects it; unchanged, it is left alone. Then each enabled
    declared source with no session is connected, and each declared source left
    out of the catalog has its reason written to its health.

    Returns ``reconnected``, ``disconnected``, ``connected`` and ``failed``
    (name -> error).
    """
    async with _lock():
        old_catalog = dict(svc.servers)
        live = list(mcp_client.persistent_sessions) if mcp_client is not None else []
        reconnected: List[str] = []
        disconnected: List[str] = []
        failed: Dict[str, Optional[str]] = {}

        async def drop(name: str) -> bool:
            ok = await mcp_client.disconnect_from_server(name)
            # Else connect_to_server short-circuits on the cached tools.
            mcp_client.tools_cache.pop(name, None)
            return ok

        # Before the swap: disconnect_from_server only stops a server in the
        # runner while its name is still in the catalog.
        for name in live:
            # An entry turning remote is dropped here too: after the swap its
            # old runner process would no longer be stopped.
            becomes_remote = (
                name in new_catalog
                and new_catalog[name].url
                and not getattr(old_catalog.get(name), "url", None)
            )
            if (
                name in new_catalog
                and svc.is_server_enabled(name)
                and not becomes_remote
            ):
                continue
            try:
                if await drop(name):
                    disconnected.append(name)
                else:
                    failed[name] = "disconnect failed"
            except Exception as exc:  # noqa: BLE001 - one server must not stop the rest
                failed[name] = f"{type(exc).__name__}: {exc}"
            _withdraw(registry, name)

        # In place: MCPClient and the runner closures hold this same dict.
        svc.servers.clear()
        svc.servers.update(new_catalog)

        for name in live:
            if name in disconnected or name in failed or name not in old_catalog:
                continue
            try:
                # Puts the saved rule paths into security-detections' new entry,
                # as the live one has, so they don't read as a change.
                svc.setup_required(name)
                if not spawn_changed(
                    svc,
                    old_catalog[name],
                    new_catalog[name],
                    mcp_client.persistent_sessions.get(name),
                ):
                    continue
                if not await drop(name):
                    # The old session is still there and would be reused.
                    failed[name] = "disconnect failed"
                    continue
                if await mcp_client.connect_to_server(name, persistent=True):
                    if registry is not None and svc.is_server_enabled(name):
                        register_connected(registry, mcp_client, name)
                    reconnected.append(name)
                    continue
                failed[name] = mcp_client.get_last_error(name) or "connect failed"
            except Exception as exc:  # noqa: BLE001 - one server must not stop the rest
                failed[name] = f"{type(exc).__name__}: {exc}"
            _withdraw(registry, name)

        connected = await _connect_sources(svc, mcp_client, registry, failed)
        await _note_left_out(svc)
        return {
            "reconnected": reconnected,
            "disconnected": disconnected,
            "connected": connected,
            "failed": failed,
        }


# --- Declared sources --------------------------------------------------------

# Source -> (what it was last dialled with, when it may be dialled again, delay).
_backoff: Dict[str, Tuple[str, float, float]] = {}
_RETRY_FIRST, _RETRY_MAX = 10.0, 300.0
_CONNECT_TIMEOUT = 30.0


async def _abandon(mcp_client, name: str, reason: str) -> None:
    """Drop a connect that timed out, and say so on the source's health."""
    from core.integrations.mcp.client import note_source_health

    try:
        await mcp_client.disconnect_from_server(name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Dropping %s after its connect timed out: %s", name, exc)
    mcp_client.tools_cache.pop(name, None)
    mcp_client.last_errors[name] = reason
    await asyncio.to_thread(note_source_health, name, False, reason)


def _dial_key(svc, server) -> str:
    """A digest of the URL and headers as resolved now: a saved credential
    changes it, so a source is retried at once rather than after its backoff."""
    url, headers = svc.remote_target(server)
    raw = json.dumps([url, sorted(headers.items())]).encode()
    return hashlib.sha256(raw).hexdigest()


async def _connect_sources(
    svc, mcp_client, registry: Optional[MCPRegistry], failed: Dict[str, Any]
) -> List[str]:
    """Connect each enabled declared source that has no session. A network
    failure backs off; a missing credential costs nothing to check again."""
    if mcp_client is None:
        return []
    connected: List[str] = []
    now = time.monotonic()
    for name, server in list(svc.servers.items()):
        if not getattr(server, "source", False) or name in failed:
            continue
        if name in mcp_client.persistent_sessions and name in mcp_client.tools_cache:
            continue
        if not svc.is_server_enabled(name):
            continue
        key = await asyncio.to_thread(_dial_key, svc, server)
        dialled, not_before, delay = _backoff.get(name, ("", 0.0, 0.0))
        if dialled == key and now < not_before:
            continue
        try:
            # Bounded: a source that never answers must not hold the lock.
            ok = await asyncio.wait_for(
                mcp_client.connect_to_server(name, persistent=True), _CONNECT_TIMEOUT
            )
        except asyncio.TimeoutError:
            ok = False
            failed[name] = f"no answer within {_CONNECT_TIMEOUT:.0f}s"
            await _abandon(mcp_client, name, failed[name])
        except Exception as exc:  # noqa: BLE001 - one source must not stop the rest
            ok, failed[name] = False, f"{type(exc).__name__}: {exc}"
        if ok:
            _backoff.pop(name, None)
            # Switched off while connecting: the disable route has withdrawn it.
            if registry is not None and svc.is_server_enabled(name):
                register_connected(registry, mcp_client, name)
            connected.append(name)
            continue
        if mcp_client.get_missing_credentials(name):
            _backoff.pop(name, None)  # dormant, not failed; its health names it
            continue
        failed.setdefault(name, mcp_client.get_last_error(name) or "connect failed")
        delay = min(max(delay * 2, _RETRY_FIRST), _RETRY_MAX)
        _backoff[name] = (key, now + delay, delay)
    return connected


async def _note_left_out(svc) -> None:
    """Write why each declared source the catalog left out is not connected."""
    from core.integrations.mcp.client import note_source_health

    for name, reason in dict(getattr(svc, "source_states", {}) or {}).items():
        _backoff.pop(name, None)
        await asyncio.to_thread(note_source_health, name, False, reason)


def _launches(row: Dict[str, Any], saved: Optional[Dict[str, str]] = None) -> Any:
    """What of ``sources.applied`` changes the catalog; a body edit does not.
    The saved integration instances (#5353) change it too."""
    sources = {n: s.get("launch_hash") for n, s in (row.get("sources") or {}).items()}
    accepted = {n: a.get("hash") for n, a in (row.get("accepted") or {}).items()}
    return sources, accepted, saved or {}


def _instances(svc) -> Dict[str, str]:
    reader = getattr(getattr(svc, "_integration_bridge", None), "instances", None)
    try:
        return reader() if reader is not None else {}
    except Exception as exc:  # noqa: BLE001 - the last catalog stands
        logger.debug("Integration instances not read: %s", exc)
        return {
            name: server.instance_of
            for name, server in svc.servers.items()
            if getattr(server, "instance_of", None)
        }


async def remerge_once(
    mcp_client, registry: Optional[MCPRegistry], last: Any = None
) -> Any:
    """One pass: re-merge when ``sources.applied`` moved since ``last``, else
    retry the sources that are not connected. Returns what to pass next time."""
    from core.sources import applied_sources

    svc = mcp_client.mcp_service
    row = await asyncio.to_thread(applied_sources)
    seen = _launches(row, await asyncio.to_thread(_instances, svc))
    if seen != last:
        new_catalog = await asyncio.to_thread(svc.load_catalog)
        result = await apply_catalog(svc, mcp_client, registry, new_catalog)
        if any(result.values()):
            logger.info("Declared sources re-merged: %s", result)
        return seen
    async with _lock():
        # A switch-off saved by another worker reaches this one's session here.
        dropped = []
        for name, server in list(svc.servers.items()):
            if (
                getattr(server, "source", False)
                and name in mcp_client.persistent_sessions
                and not svc.is_server_enabled(name)
            ):
                await mcp_client.disconnect_from_server(name)
                mcp_client.tools_cache.pop(name, None)
                _withdraw(registry, name)
                dropped.append(name)
        failed: Dict[str, Any] = {}
        connected = await _connect_sources(svc, mcp_client, registry, failed)
    if dropped or connected:
        logger.info(
            "Declared sources switched off: %s; connected: %s",
            ", ".join(dropped) or "none",
            ", ".join(connected) or "none",
        )
    return seen


async def run_declared_sources(
    mcp_client,
    registry: Optional[MCPRegistry],
    stop: Optional[asyncio.Event] = None,
) -> None:
    """Re-merge declared sources every ``sources.applied`` read, until ``stop``."""
    from core.sources import _APPLIED_TTL_SECONDS

    last: Any = _launches({})
    while stop is None or not stop.is_set():
        try:
            last = await remerge_once(mcp_client, registry, last)
        except Exception:  # noqa: BLE001 - never stops the process
            logger.exception("Declared sources: re-merge failed; retrying")
        if stop is None:
            await asyncio.sleep(_APPLIED_TTL_SECONDS)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=_APPLIED_TTL_SECONDS)
        except asyncio.TimeoutError:
            pass
