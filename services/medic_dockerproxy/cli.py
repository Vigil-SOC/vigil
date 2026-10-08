"""`python -m services.medic_dockerproxy run | check`."""

from __future__ import annotations

import asyncio
import http.client
import os
import signal
import socket
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from services.medic_dockerproxy import logs, server

P = "VIGIL_MEDIC_DOCKERPROXY_"
# K1 §6 C6 / T-15: the proxy needs no credential, so none of the agent or daemon
# tokens, the Medic API key or the database password belongs in its environment.
FORBIDDEN_ENV = (
    "AGENT_INTERNAL_TOKEN",
    "VIGIL_TOOLS_TOKEN",
    "DAEMON_WEBHOOK_TOKEN",
    "VIGIL_MEDIC_API_KEY",
    "POSTGRES_PASSWORD",
)
WILDCARDS = ("", "*", "0.0.0.0", "::", "[::]")
USAGE = "usage: python -m services.medic_dockerproxy {run|check}"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    bind: tuple[str, int]
    socket: str


def load_config(env: Mapping[str, str]) -> Config:
    bad = [k for k in FORBIDDEN_ENV if k in env]
    if bad:
        raise ConfigError(f"refusing to start: {', '.join(bad)} must not be set here")
    host, _, port = env.get(P + "BIND", "").rpartition(":")
    if host in WILDCARDS or not port.isdigit():
        raise ConfigError(f"{P}BIND must be host:port, and not a wildcard host")
    return Config((host, int(port)), env.get(P + "SOCKET", "/var/run/docker.sock"))


def _bind(hp: tuple[str, int]) -> tuple[str, int]:
    # One network's address (on Compose, the alias on medic-net), never 0.0.0.0.
    return socket.gethostbyname(hp[0]), hp[1]


def check(cfg: Config) -> int:
    """Healthy = the proxy answers and Docker behind it does: `/_ping` → 200."""
    try:
        conn = http.client.HTTPConnection(*_bind(cfg.bind), timeout=3)
        conn.request("GET", "/_ping")
        return 0 if conn.getresponse().status == 200 else 1
    except OSError:
        return 1


async def serve(cfg: Config, addr: tuple[str, int], stop: threading.Event | None):
    proxy = server.Proxy(cfg.socket)
    port = await proxy.start(*addr)
    logs.event("listening", addr=f"{addr[0]}:{port}")
    if stop is None:
        done = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):  # PID 1 ignores SIGTERM otherwise
            asyncio.get_running_loop().add_signal_handler(sig, done.set)
        await done.wait()
    else:
        await asyncio.to_thread(stop.wait)
    await proxy.stop()


def main(argv: Sequence[str], env: Mapping[str, str] | None = None, stop=None) -> int:
    if list(argv) not in (["run"], ["check"]):
        print(USAGE, file=sys.stderr)
        return 2
    env = os.environ if env is None else env  # noqa: ENV001 - the process boundary
    try:
        cfg = load_config(env)
    except ConfigError as e:
        print(f"medic-dockerproxy: {e}", file=sys.stderr)
        return 2
    if argv[0] == "check":
        return check(cfg)
    logs.setup(sys.stdout)
    asyncio.run(serve(cfg, _bind(cfg.bind), stop))
    return 0
