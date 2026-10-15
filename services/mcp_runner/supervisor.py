"""Owns each running server: its child process, stdio session and HTTP bridge.

A server lives in one long-lived task, started on PUT and cancelled on DELETE
or restart. The task spawns the child, enters a ``StreamableHTTPSessionManager``
for it (whose ``run()`` is once per manager, so a restart builds a new one) and
exits the contexts in the order it entered them. Request handlers only look the
manager up; they never enter it.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Optional

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from core.integrations.mcp.client import PersistentServerSession
from services.mcp_runner.catalog import TMP_DIR, Catalog

logger = logging.getLogger(__name__)

STDERR_TAIL_LINES = 40
EXIT_POLL_SECONDS = 0.5
# By path: a bare name would be looked up on the child env's PATH, as root.
SETPRIV = "/usr/bin/setpriv"


class ShuttingDown(Exception):
    """A start was refused because the supervisor is stopping."""


class _ChildSession(PersistentServerSession):
    """``PersistentServerSession`` with the child's stderr and process in reach.

    Only ``connect`` differs: it hands ``stdio_client`` a pipe for stderr and
    keeps the process, which the SDK does not expose, for its pid and exit code.
    """

    def __init__(
        self,
        name: str,
        params: StdioServerParameters,
        errlog_fd: int,
        start_timeout: float,
    ):
        super().__init__(name, params)
        self._errlog_fd = errlog_fd
        self._start_timeout = start_timeout
        self.process: Any = None

    async def connect(self) -> bool:
        errlog = os.fdopen(self._errlog_fd, "wb", buffering=0)
        try:
            self.stdio_context = stdio_client(self.server_params, errlog=errlog)  # type: ignore[arg-type]
            self.read_stream, self.write_stream = await self.stdio_context.__aenter__()
            # The suspended generator's frame holds the anyio Process.
            self.process = self.stdio_context.gen.ag_frame.f_locals.get("process")  # type: ignore[attr-defined]
            errlog.close()  # the child holds its own copy
            self.session_context = ClientSession(self.read_stream, self.write_stream)
            self.session = await self.session_context.__aenter__()
            # Only the handshake is timed: a scope around the whole connect
            # would close before the task groups entered under it.
            with anyio.fail_after(self._start_timeout):
                await self.session.initialize()
            self.is_connected = True
            return True
        except BaseException:
            errlog.close()
            await self._cleanup()
            raise


def _bridge(name: str, session: ClientSession) -> Server:
    """A low-level server whose tools are the child's, called straight through.

    Not through ``PersistentServerSession.call_tool``: its lock would serialise
    calls the SDK session multiplexes.
    """

    async def list_tools(ctx, params):
        return await session.list_tools(params=params)

    async def call_tool(ctx, params):
        return await session.call_tool(params.name, params.arguments)

    return Server(name, on_list_tools=list_tools, on_call_tool=call_tool)


@dataclass
class Child:
    name: str
    env: Dict[str, str]
    uid: int
    stop: anyio.Event = field(default_factory=anyio.Event)
    ready: anyio.Event = field(default_factory=anyio.Event)
    done: anyio.Event = field(default_factory=anyio.Event)
    stderr: Deque[str] = field(default_factory=lambda: deque(maxlen=STDERR_TAIL_LINES))
    state: str = "starting"
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    manager: Optional[StreamableHTTPSessionManager] = None

    def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"state": self.state, "pid": self.pid, "uid": self.uid}
        if self.state in ("failed", "exited"):
            out.update(
                exit_code=self.exit_code, error=self.error, stderr="".join(self.stderr)
            )
        return out


class Supervisor:
    def __init__(
        self,
        catalog: Catalog,
        state_dir: Path,
        uids: Dict[str, int],
        switch_uid: bool,
        start_timeout: float = 60.0,
    ):
        self.catalog = catalog
        self.state_dir = Path(state_dir)
        self.uids = uids
        self.switch_uid = switch_uid
        self.start_timeout = start_timeout
        self.children: Dict[str, Child] = {}
        self._locks: Dict[str, anyio.Lock] = {}
        self._tg: Optional[anyio.abc.TaskGroup] = None
        self._closing = False
        if not switch_uid:
            logger.warning(
                "MCP runner is not root: servers run as this process's uid, without setpriv"
            )

    async def __aenter__(self) -> "Supervisor":
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._tg is not None
        # Refuse new starts, then stop each child under its lock so a start
        # in flight finishes first and is stopped by event, not cancellation.
        # All at once: each may take its stdin grace period.
        self._closing = True
        async with anyio.create_task_group() as stops:
            for name in list(self.children):
                stops.start_soon(self._stop_locked, name)
        self._tg.cancel_scope.cancel()
        await self._tg.__aexit__(*exc)
        self._tg = None

    def _lock(self, name: str) -> anyio.Lock:
        return self._locks.setdefault(name, anyio.Lock())

    def running(self, name: str) -> Optional[Child]:
        child = self.children.get(name)
        return child if child is not None and child.state == "running" else None

    def status(self) -> Dict[str, Dict[str, Any]]:
        return {
            name: (
                self.children[name].status()
                if name in self.children
                else {"state": "stopped"}
            )
            for name in self.catalog.names()
        }

    async def put(self, name: str, env: Dict[str, str]) -> tuple[str, Child]:
        """Start ``name`` with ``env``; a no-op when it already runs with it."""
        async with self._lock(name):
            if self._closing:
                raise ShuttingDown
            current = self.running(name)
            if current is not None and current.env == env:
                return "unchanged", current
            action = "restarted" if current is not None else "started"
            await self._stop(name)
            if self.switch_uid and name not in self.uids:
                child = Child(name, env, -1, state="failed", error="no uid provisioned")
                self.children[name] = child
                return "failed", child
            child = Child(name, dict(env), self.uids.get(name, os.getuid()))
            self.children[name] = child
            assert self._tg is not None
            self._tg.start_soon(self._serve, child)
            await child.ready.wait()
            logger.info(
                "MCP server %s %s: %s (env keys: %s)",
                name,
                action,
                child.state,
                sorted(env),
            )
            return (action if child.state == "running" else "failed"), child

    async def delete(self, name: str) -> None:
        async with self._lock(name):
            await self._stop(name)
            self.children.pop(name, None)

    async def _stop_locked(self, name: str) -> None:
        async with self._lock(name):
            await self._stop(name)

    async def _stop(self, name: str) -> None:
        child = self.children.get(name)
        if child is not None and not child.done.is_set():
            child.stop.set()
            await child.done.wait()

    def _params(self, child: Child) -> StdioServerParameters:
        home = self.state_dir / child.name
        if not self.switch_uid:  # as root, provisioned at image build
            (home / TMP_DIR).mkdir(parents=True, exist_ok=True)
        launch = self.catalog.launch(child.name, child.env, str(home))
        command, args = launch.command, launch.args
        if self.switch_uid:
            args = [
                f"--reuid={child.uid}",
                f"--regid={child.uid}",
                "--clear-groups",
                "--inh-caps=-all",
                "--",
                command,
                *args,
            ]
            command = SETPRIV
        return StdioServerParameters(
            command=command, args=args, cwd=launch.cwd, env=launch.env
        )

    async def _drain(self, fd: int, child: Child, drained: anyio.Event) -> None:
        """Forward the child's stderr to the log and keep its tail."""
        buffer = b""
        try:
            while True:
                await anyio.wait_readable(fd)
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    break
                *lines, buffer = (buffer + chunk).split(b"\n")
                for line in lines:
                    text = line.decode("utf-8", "replace")
                    child.stderr.append(text + "\n")
                    logger.info("[%s] %s", child.name, text)
            if buffer:
                child.stderr.append(buffer.decode("utf-8", "replace"))
        finally:
            os.close(fd)
            drained.set()

    async def _serve(self, child: Child) -> None:
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        drained = anyio.Event()
        session: Optional[_ChildSession] = None
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(self._drain, read_fd, child, drained)
                try:
                    session = _ChildSession(
                        child.name, self._params(child), write_fd, self.start_timeout
                    )
                    write_fd = -1  # connect owns it now
                    await self._run_child(child, session)
                except Exception as exc:
                    child.state, child.error = "failed", type(exc).__name__
                    logger.warning("MCP server %s failed: %s", child.name, child.error)
                finally:
                    if write_fd != -1:
                        os.close(write_fd)
                        write_fd = -1
                    # Not under a shield: exiting the session and stdio task
                    # groups from inside a scope entered after them is out of
                    # order. Stop is an event, so nothing here is cancelled.
                    if session is not None:
                        await session.disconnect()
                    # A kill survivor could hold the pipe open.
                    with anyio.move_on_after(1):
                        await drained.wait()
                tg.cancel_scope.cancel()
        finally:
            if session is not None and session.process is not None:
                child.exit_code = session.process.returncode
            if child.state == "running":
                child.state = "stopped" if child.stop.is_set() else "exited"
            elif child.state == "starting":
                child.state = "stopped"
            child.manager = None
            child.ready.set()
            child.done.set()

    async def _run_child(self, child: Child, session: _ChildSession) -> None:
        await session.connect()
        assert session.session is not None and session.process is not None
        manager = StreamableHTTPSessionManager(
            app=_bridge(child.name, session.session),
            # Stated, not left to the SDK default, which refuses any Host but
            # localhost; the bearer middleware is what gates this surface.
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
            # A caller that never DELETEs its session must not hold it forever.
            session_idle_timeout=1800,
        )
        async with manager.run():
            child.manager = manager
            child.pid = session.process.pid
            child.state = "running"
            child.ready.set()
            while session.process.returncode is None and not child.stop.is_set():
                with anyio.move_on_after(EXIT_POLL_SECONDS):
                    await child.stop.wait()
            # Unroute before the manager stops: a request reaching a stopped
            # manager raises instead of answering.
            child.manager = None
            if not child.stop.is_set():
                logger.warning(
                    "MCP server %s exited with %s",
                    child.name,
                    session.process.returncode,
                )
