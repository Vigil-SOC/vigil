"""`python -m services.medic_gateway run | check`."""

from __future__ import annotations

import http.client
import os
import re
import socket
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from services.medic_gateway import logs, server
from services.medic_gateway.session import ViewerSession

P = "VIGIL_MEDIC_GATEWAY_"
# K1 §6 C6: none of the three agent or daemon tokens, no Medic API key, and no
# Viewer password in this environment (it comes from a file).
FORBIDDEN_ENV = (
    "AGENT_INTERNAL_TOKEN",
    "VIGIL_TOOLS_TOKEN",
    "DAEMON_WEBHOOK_TOKEN",
    "VIGIL_MEDIC_API_KEY",
    P + "VIEWER_PASSWORD",
)
CONTEXT_RE = re.compile(r"(/[A-Za-z0-9_-]+)*")
USAGE = "usage: python -m services.medic_gateway {run|check}"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    backend: tuple[str, int]
    medic: tuple[str, int]
    out_bind: tuple[str, int]
    in_bind: tuple[str, int]
    user: str
    password_file: Path
    context_path: str


def _hostport(env: Mapping[str, str], name: str) -> tuple[str, int]:
    host, _, port = env.get(P + name, "").rpartition(":")
    if not host or not port.isdigit():
        raise ConfigError(f"{P}{name} must be host:port")
    return host, int(port)


def load_config(env: Mapping[str, str]) -> Config:
    bad = [k for k in FORBIDDEN_ENV if k in env]
    if bad:
        raise ConfigError(f"refusing to start: {', '.join(bad)} must not be set here")
    context = env.get("VIGIL_CONTEXT_PATH", "")
    if not CONTEXT_RE.fullmatch(context):
        raise ConfigError("VIGIL_CONTEXT_PATH must look like /name or be empty")
    if not env.get(P + "VIEWER_USER"):
        raise ConfigError(f"{P}VIEWER_USER is required")
    pw = env.get(P + "VIEWER_PASSWORD_FILE", "/run/secrets/medic_viewer_password")
    return Config(
        _hostport(env, "BACKEND"),
        _hostport(env, "MEDIC"),
        _hostport(env, "OUT_BIND"),
        _hostport(env, "IN_BIND"),
        env[P + "VIEWER_USER"],
        Path(pw),
        context,
    )


def _bind(hp: tuple[str, int]) -> tuple[str, int]:
    # Each listener binds to one network's address (SP1 ⚑6), never 0.0.0.0, so
    # Medic can't reach the inbound side and the backend can't reach the outbound.
    return socket.gethostbyname(hp[0]), hp[1]


def build(cfg: Config) -> list:
    backend = server.Upstream(*cfg.backend, prefix=cfg.context_path)
    session = ViewerSession(backend, cfg.user, cfg.password_file)
    medic = server.Upstream(*cfg.medic, max_body=8 << 20)  # evidence export
    return [
        server.make_server("outbound", _bind(cfg.out_bind), backend, session),
        server.make_server("inbound", _bind(cfg.in_bind), medic),
    ]


def check(cfg: Config) -> int:
    """Healthy = both listeners answer. The login state is a Medic signal, not health."""
    try:
        conn = http.client.HTTPConnection(*_bind(cfg.out_bind), timeout=3)
        conn.request("GET", "/_gw/status")
        if conn.getresponse().status != 200:
            return 1
        socket.create_connection(_bind(cfg.in_bind), timeout=3).close()
    except OSError:
        return 1
    return 0


def main(argv: Sequence[str], env: Mapping[str, str] | None = None, stop=None) -> int:
    if list(argv) not in (["run"], ["check"]):
        print(USAGE, file=sys.stderr)
        return 2
    env = os.environ if env is None else env  # noqa: ENV001 - the process boundary
    try:
        cfg = load_config(env)
    except ConfigError as e:
        print(f"medic-gateway: {e}", file=sys.stderr)
        return 2
    if argv[0] == "check":
        return check(cfg)
    logs.setup(sys.stdout)
    servers = build(cfg)
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
        logs.event("listening", addr=f"{s.server_address[0]}:{s.server_address[1]}")
    (stop or threading.Event()).wait()
    for s in servers:
        s.shutdown()
        s.server_close()
    return 0
