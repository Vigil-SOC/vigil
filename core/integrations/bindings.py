"""What each tool answers (#5312) or carries (#5313): the one home for a binding.

Shipped bindings come from ``bindings.yaml`` beside this file; a person's,
stored on the tool's class row (``tool_effect_classes.binding``), replaces the
shipped one for that tool. The resolver binds every bound tool a deployment
carries, with no preference order. An act tool's binding is a carrier: what an
approved act of its kind calls on that server and how it reads each answer
(docs/acts.md).

A tool bound to ``emulation`` is an emulator (#5315): stored as
``{"emulation": true, "server": ...}``, held to the host list in
``core/response/emulation.py``. A tool has one binding: emulation, a capability,
or a carrier.
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
# Read-only lookups of an act's state on servers that do not carry the act.
STATE_READS_FILE = Path(__file__).with_name("state_reads.yaml")
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
    # What it answers; None for an act carrier.
    capability: Optional[str]
    source: Source
    # The validated carrier of an act tool (``carrier()``); None for a capability.
    carrier: Optional[Dict[str, Any]] = None


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
        for tool, value in (tools or {}).items():
            name = catalogue_name(server, tool)
            if isinstance(value, dict):
                acts = carrier(value, server, tool)
                out[name] = Binding(name, server, None, "file", acts)
            else:
                out[name] = Binding(
                    name, None if server == VIGIL else server, validate(value), "file"
                )
    clash = carrier_clash(out)
    if clash:
        raise InvalidBinding(f"bindings.yaml: {clash}")
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
    """A stored row's binding; None for ``{}``, a capability no longer known, or
    a carrier that no longer validates."""
    if not raw:
        return None
    server = raw.get("server") or None
    if "act" in raw:
        if not server or not name.startswith(f"{server}_"):
            return None
        try:
            acts = carrier(raw, server, name[len(server) + 1 :])
        except InvalidBinding as exc:
            logger.warning("carrier on %s not read: %s", name, exc)
            return None
        return Binding(name, server, None, "person", acts)
    capability = EMULATION if raw.get("emulation") is True else raw.get("capability")
    if capability not in BINDABLE:
        return None
    return Binding(name, server, capability, "person")


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


# ---- Act carriers (#5313) ---------------------------------------------------
# The form is docs/acts.md; it changes only by addition. A carrier names bare
# tools of the act tool's own server, which the server prefixes as the registry
# does. Each reading is {read: [{field: dotted.path, map: {value: state}}],
# default: state}: the first entry whose value is in its map wins.


def _key(value: Any) -> Optional[str]:
    """A read value as a map key: YAML's ``true`` and a JSON column's ``"true"``
    match alike."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _name(value: Any) -> bool:
    return (
        isinstance(value, str) and bool(value) and not any(c.isspace() for c in value)
    )


def _reading(raw: Any, part: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise InvalidBinding(f"the carrier needs {part}, a reading with a default")
    default = raw.get("default")
    if not isinstance(default, str) or not default:
        raise InvalidBinding(f"{part} needs a default state")
    entries = raw.get("read") or []
    if not isinstance(entries, list):
        raise InvalidBinding(f"{part}.read must be a list")
    read = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not _name(entry.get("field")):
            raise InvalidBinding(f"{part}.read[{i}] needs a field")
        mapped = entry.get("map")
        if not isinstance(mapped, dict) or not mapped:
            raise InvalidBinding(f"{part}.read[{i}] needs a map")
        out = {}
        for value, state in mapped.items():
            if _key(value) is None or not isinstance(state, str) or not state:
                raise InvalidBinding(f"{part}.read[{i}].map maps {value!r} to no state")
            out[_key(value)] = state
        read.append({"field": entry["field"], "map": out})
    return {"read": read, "default": default}


def read(reading: Dict[str, Any], answer: Any) -> str:
    """The state ``answer`` reads as under ``reading``."""
    for entry in reading["read"]:
        value = answer
        for step in entry["field"].split("."):
            value = value.get(step) if isinstance(value, dict) else None
        key = _key(value)
        if key is not None and key in entry["map"]:
            return entry["map"][key]
    return reading["default"]


def carrier(raw: Any, server: str, tool: str) -> Dict[str, Any]:
    """The carrier ``raw`` declares on ``server``'s act tool ``tool``, every part
    filled; or InvalidBinding naming the part missing or wrong."""
    from core.response.proposal_targets import (
        DONE_STATE,
        TARGET_KIND_NOUNS,
        UNDO_VERBS,
    )

    if not isinstance(raw, dict):
        raise InvalidBinding("a carrier is an object")
    act = raw.get("act")
    if act not in DONE_STATE:
        raise InvalidBinding(
            f"act {act!r} is not an act kind; one of {', '.join(DONE_STATE)}"
        )

    def need(part: str) -> str:
        if not _name(raw.get(part)):
            raise InvalidBinding(f"the {act} carrier needs {part}")
        return raw[part]

    def tool_of(part: str, value: Any) -> Optional[str]:
        # Bare names on the act tool's own server: a prefixed one names another.
        if value in (None, ""):
            return None
        if not _name(value):
            raise InvalidBinding(f"{part} must be a bare tool name of {server}")
        return value

    label = raw.get("carrier")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise InvalidBinding("carrier, the label, must be text")
    out: Dict[str, Any] = {
        "act": act,
        "server": server,
        "tool": tool,
        "carrier": label,
        "id_arg": need("id_arg"),
        "note_arg": need("note_arg"),
        "lookup": tool_of("lookup", raw.get("lookup")),
        "act_state": _reading(raw.get("act_state"), "act_state"),
        "state_read": None,
        "read_state": None,
        "undo": tool_of("undo", raw.get("undo")),
        "undo_from": raw.get("undo_from") or None,
    }
    if out["lookup"]:
        rows = raw.get("rows")
        if rows is not None and not _name(rows):
            raise InvalidBinding("rows must name the list the lookup answers in")
        address_arg = raw.get("address_arg")
        if address_arg is not None and not _name(address_arg):
            raise InvalidBinding("address_arg must be an argument name")
        out.update(
            lookup_arg=need("lookup_arg"),
            address_arg=address_arg,
            rows=rows,
            id=need("id"),
            name=need("name"),
            state=_reading(raw.get("state"), "state"),
            takes=None,
        )
    else:
        takes = raw.get("takes")
        if not isinstance(takes, list) or not takes:
            raise InvalidBinding(
                f"the {act} carrier needs lookup, or takes: the target kinds it acts on"
            )
        unknown = [t for t in takes if t not in TARGET_KIND_NOUNS]
        if unknown:
            raise InvalidBinding(
                f"takes names {unknown[0]!r}; one of {', '.join(TARGET_KIND_NOUNS)}"
            )
        out.update(
            lookup_arg=None,
            address_arg=None,
            rows=None,
            id=None,
            name=None,
            state=None,
            takes=list(takes),
        )
    state_read = raw.get("state_read")
    if state_read is not None:
        if not isinstance(state_read, dict) or not _name(state_read.get("arg")):
            raise InvalidBinding("state_read needs tool and arg")
        tool_read = tool_of("state_read.tool", state_read.get("tool"))
        if not tool_read:
            raise InvalidBinding("state_read needs tool and arg")
        out["state_read"] = {"tool": tool_read, "arg": state_read["arg"]}
        out["read_state"] = _reading(raw.get("read_state"), "read_state")
    if out["undo"] and act not in UNDO_VERBS:
        raise InvalidBinding(f"{act} has no undo: drop undo")
    if out["undo_from"] and (not out["undo"] or not _name(out["undo_from"])):
        raise InvalidBinding("undo_from names a field of the act's answer, beside undo")
    return out


def stored_carrier(value: Dict[str, Any]) -> Dict[str, Any]:
    """A carrier as a class row stores it: the act tool is the row's own name."""
    return {k: v for k, v in value.items() if k != "tool"}


def carrier_clash(
    found: Dict[str, Binding], involving: Optional[str] = None
) -> Optional[str]:
    """Why ``found`` holds two carriers of one act kind on one server (one of
    them ``involving`` when given), or None."""
    seen: Dict[Tuple[str, str], str] = {}
    for name, b in found.items():
        if b.carrier:
            key = (b.carrier["server"], b.carrier["act"])
            other = seen.setdefault(key, name)
            if other != name and involving in (None, name, other):
                return (
                    f"{key[0]} already carries {key[1]} through "
                    f"{name if other == involving else other}: "
                    "a server carries at most one per act kind"
                )
    return None


def carriers(act: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every carrier in force, of ``act`` when given, in file order. Where a
    server would carry one kind twice (a shipped carrier added since a person
    bound one), the person's wins, then the first."""
    found = [b for b in merged().values() if b.carrier]
    person = {(b.server, b.carrier["act"]) for b in found if b.source == "person"}
    out, seen = [], set()
    for b in found:
        key = (b.server, b.carrier["act"])
        if key in seen or (b.source == "file" and key in person):
            continue
        seen.add(key)
        if act is None or key[1] == act:
            out.append(b.carrier)
    return out


def ever_carries(tool: str) -> Optional[str]:
    """The act kind ``tool`` carries now or as shipped: a person's reversal hold
    on a shipped act tool outlives unbinding it."""
    for b in (merged().get(tool), shipped().get(tool)):
        if b is not None and b.carrier:
            return b.carrier["act"]
    return None


def refusal(found: Dict[str, Any], target: str) -> Optional[str]:
    """Why a keyless carrier can't act on ``target``, or None when it takes it."""
    from core.response.proposal_targets import TARGET_KIND_NOUNS, target_kind

    kind = target_kind(target)
    if kind in found["takes"]:
        return None
    takes = " or ".join(TARGET_KIND_NOUNS[t] for t in found["takes"])
    seen = TARGET_KIND_NOUNS.get(kind, kind)
    return f"{found['carrier'] or found['server']} takes {takes}, not {seen}"


def lookup_args(found: Dict[str, Any], target: str) -> Dict[str, str]:
    """The lookup's arguments: the address argument for an address, when named;
    else the lookup argument, filled into the lookup's template when it has one."""
    from core.response.proposal_targets import _is_ip

    address = _is_ip(target)
    if found["address_arg"] and address:
        return {found["address_arg"]: target}
    template = found.get("address_template" if address else "template")
    if template:
        return {found["lookup_arg"]: template.format(target=_quoted(target))}
    return {found["lookup_arg"]: target}


def _quoted(value: str) -> str:
    """``value`` inside a single-quoted query string: a quote or backslash escaped."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _state_reading(raw: Any, where: str) -> Dict[str, Any]:
    """A state lookup's reading: a carrier's, with at least one field to read, as
    a reading with none would read every record as its default."""
    reading = _reading(raw, f"{where} state")
    if not reading["read"]:
        raise InvalidBinding(f"{where}: state needs a field to read")
    return reading


@lru_cache(maxsize=1)
def state_reads() -> Tuple[Dict[str, Any], ...]:
    """The shipped read-only state lookups (``state_reads.yaml``), each shaped as
    a lookup carrier reads: server, act, lookup, its arguments, rows, name and
    state. A server here carries nothing: none of these is ever called to act."""
    from core.response.approval_service import CONTAINMENT_TYPES

    doc = yaml.safe_load(STATE_READS_FILE.read_text()) or {}
    if not isinstance(doc, dict):
        raise InvalidBinding("state_reads.yaml: servers must be a mapping")
    out: List[Dict[str, Any]] = []
    for server, tools in doc.items():
        if not isinstance(tools, dict) or not _name(server):
            raise InvalidBinding(f"state_reads.yaml: {server} must map its tools")
        for tool, raw in tools.items():
            where = f"state_reads.yaml: {server}.{tool}"
            if not _name(tool):
                raise InvalidBinding(f"{where} is not a bare tool name")
            if not isinstance(raw, dict) or raw.get("reads") not in CONTAINMENT_TYPES:
                raise InvalidBinding(f"{where} reads no act kind")
            for part in ("lookup_arg", "name"):
                if not _name(raw.get(part)):
                    raise InvalidBinding(f"{where} needs {part}")
            rows = raw.get("rows")
            if rows is not None and not _name(rows):
                raise InvalidBinding(f"{where}: rows must name the list it answers in")
            templates = {}
            for part in ("template", "address_template"):
                value = raw.get(part)
                if value is not None and (
                    not isinstance(value, str) or "{target}" not in value
                ):
                    raise InvalidBinding(f"{where}: {part} must hold {{target}}")
                if value is not None:
                    try:
                        value.format(target="x")
                    except (KeyError, IndexError, ValueError) as exc:
                        raise InvalidBinding(
                            f"{where}: {part} has a field besides {{target}}"
                        ) from exc
                templates[part] = value
            out.append(
                {
                    "server": server,
                    "act": raw["reads"],
                    "lookup": tool,
                    "lookup_arg": raw["lookup_arg"],
                    "address_arg": None,
                    **templates,
                    "rows": rows,
                    "name": raw["name"],
                    "state": _state_reading(raw.get("state"), where),
                }
            )
    return tuple(out)
