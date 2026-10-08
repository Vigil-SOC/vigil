"""Settings Medic reads from its environment (prefix `VIGIL_MEDIC_`, A1)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

ENABLED_VAR = "VIGIL_MEDIC_ENABLED"
DATA_DIR_VAR = "VIGIL_MEDIC_DATA_DIR"
SHAPE_VAR = "VIGIL_MEDIC_INSTALL_SHAPE"
AGENT_WORKER_VAR = "VIGIL_MEDIC_AGENT_WORKER_ADDR"
# A3-3: on Helm, a host:port Medic's egress policy must block (the chart sets it).
POLICY_PROBE_VAR = "VIGIL_MEDIC_POLICY_PROBE_ADDR"

SHAPES = ("start_sh", "compose", "helm")
DEFAULT_SHAPE = "compose"  # PROVISIONAL (S4-2): S6/S7/S8 set it per shape
# Where the agent worker's /readyz listens (port 6990), per shape (PROVISIONAL S4-3).
AGENT_WORKER_DEFAULT = {
    "start_sh": "127.0.0.1:6990",
    "compose": "agent-worker:6990",
    "helm": "agent-worker:6990",
}
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


def policy_probe_addr(env: Mapping[str, str], shape: str) -> tuple[str, int] | None:
    """Helm only, and required there: no target means no proof (A3-3, fail closed).

    Compose isolates Medic by Docker network (`medic-net`); host-native can't be
    isolated at all (C3 §4.7). Neither has a NetworkPolicy to test."""
    if shape != "helm":
        return None
    value = (env.get(POLICY_PROBE_VAR) or "").strip()
    if not value:
        raise ConfigError(
            f"{POLICY_PROBE_VAR} is required on Helm: it names the host:port "
            "Medic's NetworkPolicy must block (the chart sets it)"
        )
    return _host_port(POLICY_PROBE_VAR, value)
