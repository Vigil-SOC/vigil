"""Settings Medic reads from its environment (prefix `VIGIL_MEDIC_`, A1)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

ENABLED_VAR = "VIGIL_MEDIC_ENABLED"
DATA_DIR_VAR = "VIGIL_MEDIC_DATA_DIR"

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
