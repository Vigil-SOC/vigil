"""Settings Medic reads from its environment (prefix `VIGIL_MEDIC_`, A1)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

ENABLED_VAR = "VIGIL_MEDIC_ENABLED"
DATA_DIR_VAR = "VIGIL_MEDIC_DATA_DIR"
SHAPE_VAR = "VIGIL_MEDIC_INSTALL_SHAPE"
AGENT_WORKER_VAR = "VIGIL_MEDIC_AGENT_WORKER_ADDR"
AGENT_SERVE_VAR = "VIGIL_MEDIC_AGENT_SERVE_ADDR"
# A3-3: on Helm, a host:port Medic's egress policy must block (the chart sets it).
POLICY_PROBE_VAR = "VIGIL_MEDIC_POLICY_PROBE_ADDR"
# ...and a host:port it allows (the gateway): the positive control.
POLICY_CONTROL_VAR = "VIGIL_MEDIC_POLICY_CONTROL_ADDR"

SHAPES = ("start_sh", "compose", "helm")
DEFAULT_SHAPE = "compose"  # PROVISIONAL (S4-2): S6/S7/S8 set it per shape
# Where the agent worker's /readyz listens (port 6990), per shape (PROVISIONAL S4-3).
AGENT_WORKER_DEFAULT = {
    "start_sh": "127.0.0.1:6990",
    "compose": "agent-worker:6990",
    "helm": "agent-worker:6990",
}
# Where agent-serve's /readyz listens (6989). Compose only by default: agent-serve
# is on medic-net there (S6). Helm's egress policy has no rule for it yet (S7 → D6)
# and host-native is S11's, so there it is read only if the variable names it.
AGENT_SERVE_DEFAULT = {"compose": "agent-serve:6989"}
# host:port only: no scheme, path, userinfo or IPv6 brackets to smuggle anything in.
_ADDR = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?):([0-9]{1,5})$")


class ConfigError(ValueError):
    """A setting Medic can't run with; the message names the variable."""


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"", "false", "0", "no", "off"}


def flag_value(env: Mapping[str, str]) -> str | None:
    return env.get(ENABLED_VAR)


def is_enabled(env: Mapping[str, str]) -> bool:
    """The master switch (C8 rule 1). Anything not clearly "on" is off: fail safe."""
    return (flag_value(env) or "").strip().lower() in _TRUE


def is_recognised(value: str | None) -> bool:
    return value is None or value.strip().lower() in _TRUE | _FALSE


def default_data_dir(platform: str) -> Path:
    # C4 §6.2 / A1. In the image the `medic_data` volume or PVC mounts here too.
    if platform == "darwin":
        return Path("/Library/Application Support/vigil-medic")
    return Path("/var/lib/vigil-medic")


def data_dir(env: Mapping[str, str], platform: str) -> Path:
    value = env.get(DATA_DIR_VAR)
    return Path(value) if value else default_data_dir(platform)


def install_shape(env: Mapping[str, str]) -> str:
    value = (env.get(SHAPE_VAR) or DEFAULT_SHAPE).strip()
    if value not in SHAPES:
        # The value isn't echoed: a mis-pasted secret would land in the log.
        raise ConfigError(f"{SHAPE_VAR} is not one of {', '.join(SHAPES)}")
    return value


def _host_port(var: str, value: str) -> tuple[str, int]:
    match = _ADDR.match(value)
    if not match or not 0 < int(match.group(2)) < 65536:
        raise ConfigError(f"{var} is not host:port (value not shown)")
    return match.group(1), int(match.group(2))


def agent_worker_addr(env: Mapping[str, str], shape: str) -> tuple[str, int]:
    value = (env.get(AGENT_WORKER_VAR) or AGENT_WORKER_DEFAULT[shape]).strip()
    return _host_port(AGENT_WORKER_VAR, value)


def agent_serve_addr(env: Mapping[str, str], shape: str) -> tuple[str, int] | None:
    value = (env.get(AGENT_SERVE_VAR) or AGENT_SERVE_DEFAULT.get(shape, "")).strip()
    return _host_port(AGENT_SERVE_VAR, value) if value else None


def _helm_addr(env: Mapping[str, str], shape: str, var: str, what: str):
    if shape != "helm":
        return None
    value = (env.get(var) or "").strip()
    if not value:
        raise ConfigError(
            f"{var} is required on Helm: it names {what} (the chart sets it)"
        )
    return _host_port(var, value)


def policy_probe_addr(env: Mapping[str, str], shape: str) -> tuple[str, int] | None:
    """Helm only, and required there: no target means no proof (A3-3, fail closed).

    Compose isolates Medic by Docker network (`medic-net`); host-native can't be
    isolated at all (C3 §4.7). Neither has a NetworkPolicy to test."""
    return _helm_addr(
        env, shape, POLICY_PROBE_VAR, "the host:port Medic's NetworkPolicy must block"
    )


def policy_control_addr(env: Mapping[str, str], shape: str) -> tuple[str, int] | None:
    """The host:port Medic's policy allows: a dropped probe only counts if this connects."""
    return _helm_addr(
        env, shape, POLICY_CONTROL_VAR, "a host:port Medic's NetworkPolicy allows"
    )


# Medic's API (X2, S9). The port is X2's default; the address is loopback unless
# the shape says Medic is inside its own container or pod, where Compose's
# networks and Helm's NetworkPolicy are the fence (C3). On Compose it is narrowed
# further to the one network that reaches the gateway (api_bind_peer). Fail
# safe: a host-native Medic with no shape set still binds loopback only.
API_PORT_VAR = "VIGIL_MEDIC_API_PORT"
API_KEY_FILE_VAR = "VIGIL_MEDIC_API_KEY_FILE"
# Compose: listen only on the network that reaches this name, the gateway's
# medic-private alias, not on medic-net beside the agents (K1 §6 G3).
API_BIND_PEER_VAR = "VIGIL_MEDIC_API_BIND_PEER"
COMPOSE_BIND_PEER = "medic-gateway-out"
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
API_PORT_DEFAULT = 8470
# Where Compose mounts the `medic_api_key` secret.
COMPOSE_KEY_FILE = Path("/run/secrets/medic_api_key")
_CONTAINER_SHAPES = ("compose", "helm")
_PORT = re.compile(r"[0-9]{1,5}")


def _explicit_shape(env: Mapping[str, str]) -> str:
    return (env.get(SHAPE_VAR) or "").strip()


def api_bind(env: Mapping[str, str]) -> tuple[str, int]:
    value = env.get(API_PORT_VAR)
    port = API_PORT_DEFAULT
    if value is not None:
        if not _PORT.fullmatch(value) or not 0 < int(value) < 65536:
            raise ConfigError(f"{API_PORT_VAR} is not a port number (value not shown)")
        port = int(value)
    if _explicit_shape(env) in _CONTAINER_SHAPES:
        return "0.0.0.0", port
    return "127.0.0.1", port


def api_key_file(env: Mapping[str, str], data_dir: Path) -> Path | None:
    """The file holding X-Medic-Key. Host-native: `<data dir>/run/api_key`, which
    the restart loop writes from what start.sh hands it. Helm: the chart names it."""
    value = (env.get(API_KEY_FILE_VAR) or "").strip()
    if value:
        return Path(value)
    shape = _explicit_shape(env)
    if shape == "compose":
        return COMPOSE_KEY_FILE
    if shape == "helm":
        return None
    return data_dir / "run" / "api_key"


def api_bind_peer(env: Mapping[str, str]) -> str | None:
    value = (env.get(API_BIND_PEER_VAR) or "").strip()
    if value:
        if not _HOST.fullmatch(value):
            raise ConfigError(
                f"{API_BIND_PEER_VAR} is not a host name (value not shown)"
            )
        return value
    return COMPOSE_BIND_PEER if _explicit_shape(env) == "compose" else None
