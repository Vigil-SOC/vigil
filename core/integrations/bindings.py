"""Which capability each tool answers (#5312): the one home for a binding.

Shipped bindings come from ``bindings.yaml`` beside this file; a person's,
stored on the tool's class row (``tool_effect_classes.binding``), replaces the
shipped one for that tool. The resolver binds every bound tool a deployment
carries, with no preference order.

A tool bound to ``emulation`` is an emulator (#5315): stored as
``{"emulation": true, "server": ...}``, held to the host list in
``core/response/emulation.py``. A tool has one binding, emulation or a capability.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

SHIPPED_FILE = Path(__file__).with_name("bindings.yaml")
# Vigil's own tools sit under this key and bind by bare name.
VIGIL = "vigil"
EMULATION = "emulation"

# Every capability a need may name, and a person may bind.
CAPABILITY_NAMES: Tuple[str, ...] = (
    "telemetry_search",
    "indicator_lookup",
    "findings_search",
    "entity_recall",
    "shell",
    "get_finding",
    "detection_coverage",
    "intel_observable",
    "intel_indicator",
    "intel_entities",
    "intel_relationships",
    "identity_lookup",
    "sandbox_hash_lookup",
    "sandbox_report",
    "topology_graph",
    "topology_focus",
    "topology_legend",
    EMULATION,
)
BINDABLE: Tuple[str, ...] = CAPABILITY_NAMES

Source = Literal["file", "person"]


@dataclass(frozen=True)
class Binding:
    # The catalogue name, {server}_{tool} or a Vigil tool's bare name.
    tool: str
    # The integration behind it; None for Vigil's own.
    server: Optional[str]
    capability: str
    source: Source


class InvalidBinding(ValueError):
    pass


def validate(capability: Any) -> str:
    """The capability, or InvalidBinding naming what is not one."""
    if not isinstance(capability, str) or capability not in BINDABLE:
        raise InvalidBinding(
            f"{capability!r} is not a capability; one of {', '.join(BINDABLE)}"
        )
    return capability


def stored_form(capability: str, server: Optional[str]) -> dict:
    """What a class row stores for a binding to ``capability``."""
    if capability == EMULATION:
        return {"emulation": True, "server": server}
    return {"capability": capability, "server": server}


def catalogue_name(server: str, tool: str) -> str:
    # The registry prefixes every outside tool, even one already carrying it.
    return tool if server == VIGIL else f"{server}_{tool}"


@lru_cache(maxsize=1)
def shipped() -> Dict[str, Binding]:
    """The shipped file's bindings by catalogue name, in file order."""
    doc = yaml.safe_load(SHIPPED_FILE.read_text()) or {}
    out: Dict[str, Binding] = {}
    for server, tools in doc.items():
        for tool, capability in (tools or {}).items():
            name = catalogue_name(server, tool)
            out[name] = Binding(
                name, None if server == VIGIL else server, validate(capability), "file"
            )
    return out


_TTL_SECONDS = 10.0
# (read at, tool name -> stored binding, whether any read has succeeded)
_stored_cache: Tuple[float, Dict[str, dict], bool] = (float("-inf"), {}, False)


def _read_stored() -> Dict[str, dict]:
    from sqlalchemy import select

    from core.storage.models import ToolEffectClass
    from core.storage.unit_of_work import unit_of_work

    query = select(ToolEffectClass.tool_name, ToolEffectClass.binding).where(
        ToolEffectClass.binding.isnot(None)
    )
    with unit_of_work() as session:
        return {name: b for name, b in session.execute(query).all()}


def stored(*, fresh: bool = False) -> Dict[str, dict]:
    """Each class row's binding, through a short cache as ``site.applied`` is
    read: every process re-reads at most every 10 s; a failed read keeps the last."""
    global _stored_cache
    now = time.monotonic()
    at, value, ok = _stored_cache
    if fresh or now - at >= _TTL_SECONDS:
        try:
            value, ok = _read_stored(), True
        except Exception as exc:  # noqa: BLE001
            logger.debug("tool bindings not read; keeping last: %s", exc)
        _stored_cache = (now, value, ok)
    return value


def invalidate() -> None:
    """Make the next read in this process go to the store."""
    global _stored_cache
    _stored_cache = (float("-inf"), _stored_cache[1], _stored_cache[2])


def merged(rows: Optional[Dict[str, dict]] = None) -> Dict[str, Binding]:
    """Every binding in force: a stored row's over the file's, file order first."""
    out = dict(shipped())
    for name, raw in sorted((stored() if rows is None else rows).items()):
        person = from_row(name, raw)
        if person is None:
            out.pop(name, None)  # {}: a person bound nothing
        else:
            out[name] = person
    return out


def from_row(name: str, raw: Optional[dict]) -> Optional[Binding]:
    """A stored row's binding; None for ``{}`` or a capability no longer known."""
    if not raw:
        return None
    capability = EMULATION if raw.get("emulation") is True else raw.get("capability")
    if capability not in BINDABLE:
        return None
    return Binding(name, raw.get("server") or None, capability, "person")


def tools_for(capability: str, *, fresh: bool = False) -> List[Binding]:
    """Every tool bound to ``capability``, in force now."""
    rows = stored(fresh=True) if fresh else None
    return [b for b in merged(rows).values() if b.capability == capability]


def servers_for(capability: str) -> List[str]:
    """Each outside server some tool of which answers ``capability``."""
    return list(dict.fromkeys(b.server for b in tools_for(capability) if b.server))


def emulators(*, fresh: bool = False) -> frozenset:
    """Every tool bound as an emulator now, by catalogue name. ``fresh`` reads
    the store past the cache, as a call's host check does."""
    return frozenset(b.tool for b in tools_for(EMULATION, fresh=fresh))


def is_emulator(tool: str, *, fresh: bool = False) -> bool:
    return tool in emulators(fresh=fresh)
