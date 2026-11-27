"""The declared site (#5209): ``site/*.md`` in the declared directory.

The single home of the Declared Site. The strict parser, the one Selector
matcher every reader calls, owner resolution against active teams, and the
cached read of the ``site.applied`` row the daemon writes all live here;
readers import from this module and keep no second model of the site.

Each file's YAML front matter may hold five lists:

- ``zones``: ``name``, ``selectors``, ``description``
- ``crown_jewels``: ``name``, ``mission``, ``selectors``, ``zone``, ``why``
- ``owners``: ``selectors``, ``team``
- ``blind_spots``: ``selectors``, ``what`` (what Vigil cannot see there)
- ``telemetry``: ``name``, ``covers`` (#5291) -- telemetry this site carries
  beyond the shipped hunt ``data_domains``. Hunts may file evidence under the
  name and it grades as telemetry. The name becomes a ``source_system`` value,
  so it matches ``TELEMETRY_NAME``, is declared once, and is neither
  ``undeclared`` nor a name already graded ``not_evidence``.

A Selector is ``cidr:<range>`` (matches ``ip:`` keys) or an Entity Key whose
type is in ``ENTITY_KEY_TYPES``, normalised as ``normalise_key`` mints it,
where ``*`` in the value matches any run of characters. No other wildcard and
no regex. Anything else refuses the whole revision with file and line.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import yaml

from core.frontmatter import (
    FrontmatterError,
    frontmatter_alias_line,
    frontmatter_node,
    split_frontmatter,
)
from core.intent import IntentRejected
from core.memory.entity_keys import normalise_key
from core.memory.recall_contract import ENTITY_KEY_TYPES

logger = logging.getLogger(__name__)

SITE_APPLIED_KEY = "site.applied"
SITE_DIR_NAME = "site"
# The ceiling on one revision. Constants on purpose: no setting, no env var.
MAX_SITE_BYTES = 512 * 1024
MAX_SITE_ITEMS = 2000

# Per list: field -> required. ``selectors`` is a list of strings, the rest strings.
KINDS: Dict[str, Dict[str, bool]] = {
    "zones": {"name": True, "selectors": True, "description": False},
    "crown_jewels": {
        "name": True,
        "mission": True,
        "selectors": True,
        "zone": False,
        "why": False,
    },
    "owners": {"selectors": True, "team": True},
    "blind_spots": {"selectors": True, "what": True},
    "telemetry": {"name": True, "covers": True},
}

# A telemetry name is a source_system value other code joins on.
TELEMETRY_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

NOTHING_DECLARED = "Nothing is declared: VIGIL_INTENT_DIR is not set"

SiteRejected = IntentRejected  # same shape: file, line, reason

_EMPTY_FRONT = re.compile(r"\A---[ \t]*\r?\n---[ \t]*(\r?\n|\Z)")


def empty_items() -> Dict[str, List[Dict[str, Any]]]:
    return {kind: [] for kind in KINDS}


# --- Selectors ---------------------------------------------------------------


def parse_selector(raw: str) -> str:
    """The stored form of a selector; ``ValueError`` names why it is refused."""
    kind, sep, value = raw.strip().partition(":")
    kind = kind.strip().lower()
    if not sep or not value.strip():
        raise ValueError(f"selector {raw!r} is not <kind>:<value>")
    if kind == "cidr":
        try:
            return f"cidr:{ipaddress.ip_network(value.strip(), strict=False)}"
        except ValueError as exc:
            raise ValueError(f"selector {raw!r}: {exc}") from None
    if kind not in ENTITY_KEY_TYPES:
        raise ValueError(
            f"selector {raw!r}: kind {kind!r} is not cidr or an Entity Key type "
            f"({', '.join(ENTITY_KEY_TYPES)})"
        )
    key = normalise_key(raw)
    if not key.partition(":")[2]:
        raise ValueError(f"selector {raw!r} has no value")
    return key


@functools.lru_cache(maxsize=4096)
def _network(value: str) -> ipaddress._BaseNetwork:
    return ipaddress.ip_network(value, strict=False)


@functools.lru_cache(maxsize=4096)
def _pattern(value: str) -> "re.Pattern[str]":
    # ``*`` is the only wildcard; everything else is literal.
    return re.compile(".*".join(map(re.escape, value.split("*"))), re.DOTALL)


def selector_matches(selector: str, key: str) -> bool:
    """Whether a stored-form selector matches one normalised Entity Key."""
    kind, _, value = selector.partition(":")
    key_kind, _, key_value = key.partition(":")
    if kind == "cidr":
        if key_kind != "ip":
            return False
        try:
            return ipaddress.ip_address(key_value) in _network(value)
        except ValueError:
            return False
    if kind != key_kind:
        return False
    if "*" in value:
        return _pattern(value).fullmatch(key_value) is not None
    return value == key_value


def selectors_overlap(a: str, b: str) -> bool:
    """Whether two selectors can name the same entity, as far as Python can tell.

    Identical strings, overlapping ranges, a range holding an ``ip:`` key, or a
    pattern matching a plain key. Two different patterns never overlap here.
    """
    if a == b:
        return True
    ka, kb = a.partition(":")[0], b.partition(":")[0]
    if ka == kb == "cidr":
        na, nb = _network(a[5:]), _network(b[5:])
        return na.version == nb.version and na.overlaps(nb)
    for one, other in ((a, b), (b, a)):
        value = other.partition(":")[2]
        if other.partition(":")[0] != "cidr" and "*" not in value:
            if selector_matches(one, other):
                return True
    return False


# --- Parser ------------------------------------------------------------------


def _mapping(node: Any) -> Dict[str, Tuple[Any, Any]]:
    """Key -> (key node, value node) of a composed mapping node."""
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {str(k.value): (k, v) for k, v in node.value}


def _parse_file(name: str, content: str) -> Dict[str, List[Dict[str, Any]]]:
    try:
        front, _ = split_frontmatter(content)
    except FrontmatterError as exc:
        problem = getattr(exc.__cause__, "problem", None)
        raise SiteRejected(
            name, exc.line, f"invalid YAML: {problem}" if problem else str(exc)
        ) from exc
    if front is None:
        if not _EMPTY_FRONT.match(content):
            raise SiteRejected(name, 1, "no YAML front matter")
        front = {}  # "---" then "---": declares nothing
    # Aliases would let a small file expand past the ceiling, and merge keys
    # would leave values with no line of their own; refuse both.
    alias = frontmatter_alias_line(content)
    if alias:
        raise SiteRejected(name, alias, "YAML anchors and aliases are not allowed")
    root, first = frontmatter_node(content)

    def line(node: Any) -> int:
        return first + node.start_mark.line if node is not None else first

    def refuse_duplicates(node: Any) -> None:
        # safe_load keeps the last of two equal keys; a strict parser says so.
        if isinstance(node, yaml.MappingNode):
            seen: set = set()
            for key_node, value_node in node.value:
                if key_node.value in seen:
                    raise SiteRejected(
                        name, line(key_node), f"duplicate key {key_node.value!r}"
                    )
                seen.add(key_node.value)
                refuse_duplicates(value_node)
        elif isinstance(node, yaml.SequenceNode):
            for child in node.value:
                refuse_duplicates(child)

    refuse_duplicates(root)
    keys = _mapping(root)

    items = empty_items()
    for kind, entries in front.items():
        key_node, list_node = keys.get(str(kind), (None, None))
        if kind not in KINDS:
            raise SiteRejected(
                name,
                line(key_node),
                f"unknown key {kind!r}; expected one of {', '.join(KINDS)}",
            )
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise SiteRejected(name, line(key_node), f"{kind} must be a list")
        fields = KINDS[kind]
        for index, entry in enumerate(entries):
            entry_node = list_node.value[index]
            at = line(entry_node)
            if not isinstance(entry, dict):
                raise SiteRejected(name, at, f"{kind}[{index}] must be a mapping")
            entry_keys = _mapping(entry_node)
            item: Dict[str, Any] = {}
            for field, value in entry.items():
                field_key, field_value = entry_keys.get(str(field), (None, None))
                where = line(field_key or entry_node)
                if field not in fields:
                    raise SiteRejected(
                        name,
                        where,
                        f"{kind}: unknown key {field!r}; expected one of "
                        f"{', '.join(fields)}",
                    )
                if field == "selectors":
                    if not isinstance(value, list) or not value:
                        raise SiteRejected(
                            name, where, f"{kind}.selectors must be a non-empty list"
                        )
                    selectors = []
                    for i, raw in enumerate(value):
                        sel_at = line(field_value.value[i])
                        if not isinstance(raw, str):
                            raise SiteRejected(
                                name, sel_at, f"selector {raw!r} is not a string"
                            )
                        try:
                            selectors.append(parse_selector(raw))
                        except ValueError as exc:
                            raise SiteRejected(name, sel_at, str(exc)) from None
                    item["selectors"] = list(dict.fromkeys(selectors))
                else:
                    if not isinstance(value, str) or not value.strip():
                        raise SiteRejected(
                            name, where, f"{kind}.{field}: {value!r} is not text"
                        )
                    item[field] = value.strip()
            missing = [
                f for f, required in fields.items() if required and f not in item
            ]
            if missing:
                raise SiteRejected(
                    name, at, f"{kind}[{index}] is missing {', '.join(missing)}"
                )
            if kind == "telemetry":
                _check_telemetry(name, at, item["name"])
            items[kind].append({**item, "file": name, "line": at})
    return items


def _check_telemetry(file: str, at: int, telemetry: str) -> None:
    # Lazy: source_tier reads the applied site back through this module.
    from core.memory.source_tier import InvestigationKind, SourceTier, shipped_tier

    if not TELEMETRY_NAME.match(telemetry):
        raise SiteRejected(
            file,
            at,
            f"telemetry name {telemetry!r} must match {TELEMETRY_NAME.pattern}",
        )
    if telemetry == "undeclared":
        raise SiteRejected(
            file, at, "telemetry name 'undeclared' is reserved for unvouched sources"
        )
    # The shipped maps only: the site lookup can only grade telemetry, and
    # reading it here would put a database read in the parser.
    if shipped_tier(telemetry, InvestigationKind.HUNT) is SourceTier.NOT_EVIDENCE:
        raise SiteRejected(
            file,
            at,
            f"telemetry name {telemetry!r} is a source Vigil grades not_evidence",
        )


def parse_site(files: Sequence[Tuple[str, str]]) -> Dict[str, List[Dict[str, Any]]]:
    """All items of one revision, or :class:`SiteRejected` for the whole of it.

    ``files`` is ``(name, text)`` in name order, ``name`` as ``site/<file>.md``.
    """
    size = sum(len(text.encode("utf-8")) for _, text in files)
    if size > MAX_SITE_BYTES:
        raise SiteRejected(
            SITE_DIR_NAME,
            1,
            f"{size} bytes is over the ceiling of {MAX_SITE_BYTES} bytes",
        )
    items = empty_items()
    for name, text in files:
        for kind, found in _parse_file(name, text).items():
            items[kind].extend(found)
    count = sum(len(v) for v in items.values())
    if count > MAX_SITE_ITEMS:
        raise SiteRejected(
            SITE_DIR_NAME, 1, f"{count} items is over the ceiling of {MAX_SITE_ITEMS}"
        )
    zones: Dict[str, Dict[str, Any]] = {}
    for zone in items["zones"]:
        if zone["name"] in zones:
            first = zones[zone["name"]]
            raise SiteRejected(
                zone["file"],
                zone["line"],
                f"zone {zone['name']!r} is already declared at "
                f"{first['file']}:{first['line']}",
            )
        zones[zone["name"]] = zone
    telemetry: Dict[str, Dict[str, Any]] = {}
    for entry in items["telemetry"]:
        if entry["name"] in telemetry:
            first = telemetry[entry["name"]]
            raise SiteRejected(
                entry["file"],
                entry["line"],
                f"telemetry {entry['name']!r} is already declared at "
                f"{first['file']}:{first['line']}",
            )
        telemetry[entry["name"]] = entry
    for jewel in items["crown_jewels"]:
        if "zone" in jewel and jewel["zone"] not in zones:
            raise SiteRejected(
                jewel["file"],
                jewel["line"],
                f"crown jewel {jewel['name']!r} names zone {jewel['zone']!r}, "
                "which no file declares",
            )
    return items


# --- The applied row ---------------------------------------------------------

_APPLIED_TTL_SECONDS = 10.0
# (read at, value, whether any read in this process has succeeded)
_applied_cache: Tuple[float, Dict[str, Any], bool] = (float("-inf"), {}, False)


class SiteUnreadable(Exception):
    """``site.applied`` could not be read and no earlier read is kept."""


def applied_site(*, fresh: bool = False, strict: bool = False) -> Dict[str, Any]:
    """The ``site.applied`` row through a short cache; ``{}`` when absent.

    Every process re-reads it at most every 10 s, so an applied revision
    reaches each backend replica without a restart. A failed read keeps the
    last value read. ``strict`` raises :class:`SiteUnreadable` when no read has
    succeeded yet, so a hold can tell "unread" from "no row".
    """
    global _applied_cache
    now = time.monotonic()
    at, value, ok = _applied_cache
    if fresh or now - at >= _APPLIED_TTL_SECONDS:
        try:
            from core.storage.config_service import get_config_service

            row = get_config_service().read_system_config(SITE_APPLIED_KEY)
            value, ok = (row if isinstance(row, dict) else {}), True
        except Exception as exc:  # noqa: BLE001
            logger.debug("site.applied not read; keeping last: %s", exc)
        _applied_cache = (now, value, ok)
    if strict and not ok:
        raise SiteUnreadable("site.applied could not be read")
    return value


def declared(row: Optional[Dict[str, Any]] = None) -> bool:
    row = applied_site() if row is None else row
    return bool(row.get("directory")) and bool(row.get("revision"))


def match(
    keys: Iterable[str], row: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Every declared item a selector of which matches one of ``keys``.

    The one matcher every reader calls. Keys are normalised here; each match
    carries the key, the list it is from (``kind``), the selector, the item
    with its file and line, and the revision. Overlaps all apply.
    """
    row = applied_site() if row is None else row
    if not declared(row):
        return []
    items = row.get("items") or {}
    revision = row.get("revision")
    found: List[Dict[str, Any]] = []
    for key in dict.fromkeys(normalise_key(str(k)) for k in keys):
        if not key:
            continue
        for kind in KINDS:
            for item in items.get(kind) or []:
                selector = next(
                    (
                        s
                        for s in item.get("selectors") or []
                        if selector_matches(s, key)
                    ),
                    None,
                )
                if selector:
                    found.append(
                        {
                            "key": key,
                            "kind": kind,
                            "selector": selector,
                            "revision": revision,
                            **item,
                        }
                    )
    return found


# --- Crown jewels a value names (#5227) --------------------------------------


def _value_keys(value: str) -> Tuple[List[str], Optional[str]]:
    """The Entity Keys a bare value is tried as, and its ``cidr:`` form if a range."""
    value = value.strip()
    if not value:
        return [], None
    kind = value.partition(":")[0].lower()
    keys = [normalise_key(value)] if kind in ENTITY_KEY_TYPES else []
    keys += [normalise_key(f"host:{value}"), normalise_key(f"user:{value}")]
    try:
        net = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return keys, None
    if net.num_addresses == 1:
        return keys + [normalise_key(f"ip:{net.network_address}")], None
    return keys, f"cidr:{net}"


def crown_jewel(
    values: Iterable[str],
    row: Optional[Dict[str, Any]] = None,
    *,
    exact: bool = False,
) -> Optional[Dict[str, Any]]:
    """The first declared crown jewel one of ``values`` names, with its revision.

    A value is an Entity Key or a bare string, tried as a host, a user and,
    when it parses, an address; a range names a crown jewel whose ``ip:`` or
    ``cidr:`` selector it overlaps. ``exact`` skips selectors holding ``*``,
    so a name chosen to fit a pattern does not count (outside calls).
    """
    row = applied_site() if row is None else row
    if not declared(row):
        return None
    jewels = (row.get("items") or {}).get("crown_jewels") or []
    for value in dict.fromkeys(str(v) for v in values if isinstance(v, str)):
        keys, cidr = _value_keys(value)
        for jewel in jewels:
            selectors = [
                s for s in jewel.get("selectors") or [] if not (exact and "*" in s)
            ]
            if any(selector_matches(s, k) for s in selectors for k in keys if k) or (
                cidr
                and any(
                    selectors_overlap(s, cidr)
                    for s in selectors
                    if s.startswith(("cidr:", "ip:"))
                )
            ):
                return {**jewel, "value": value, "revision": row.get("revision")}
    return None


# --- Owners, resolved when read ----------------------------------------------


def _active_teams() -> Dict[str, Dict[str, str]]:
    """Lower-cased name -> ``{team_id, name}`` of every non-archived team."""
    from sqlalchemy import select

    from core.storage.models import Team
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        rows = session.execute(
            select(Team.team_id, Team.name).where(Team.archived_at.is_(None))
        ).all()
    return {name.lower(): {"team_id": tid, "name": name} for tid, name in rows}


def resolve_owner(
    owner: Dict[str, Any], teams: Dict[str, Dict[str, str]]
) -> Dict[str, Any]:
    team = teams.get(str(owner.get("team", "")).lower())
    if team:
        return {**owner, "team_id": team["team_id"], "resolved": team["name"]}
    return {**owner, "unassigned": f"no team named {owner.get('team')}"}


def site_view(
    row: Optional[Dict[str, Any]] = None,
    *,
    report: bool = True,
    proposal: bool = True,
) -> Dict[str, Any]:
    """The applied site, the open proposal and the site questions (#5229).

    The proposal and the questions are ``core/site_proposal.py``'s; a failure
    there leaves them out and the site still reads. ``proposal`` False skips
    both, for a caller that wants only the items and their owners.
    """
    row = applied_site(fresh=True) if row is None else row
    view = _site_items_view(row, report=report)
    if not proposal:
        return view
    from core import site_proposal

    extra: Dict[str, Any] = {"proposal": None, "site_questions": []}
    for field, read in (
        ("proposal", lambda: site_proposal.proposal_view(row)),
        ("site_questions", lambda: site_proposal.site_questions(view, row)),
    ):
        try:
            extra[field] = read()
        except Exception as exc:  # noqa: BLE001 - the site still reads
            logger.error("site %s not read: %s", field, type(exc).__name__)
    return {**view, **extra}


def _site_items_view(row: Dict[str, Any], *, report: bool) -> Dict[str, Any]:
    """``GET /api/config/site`` and the /mcp ``get_site_context`` tool.

    Owners resolve now, against active teams ignoring case, so a team created
    after the commit names its items with no new commit. Each zone and crown
    jewel lists the owners whose selectors overlap its own. ``report`` adds the
    latest Day 1 Report's site checks and evidence; both callers already
    require platform.read, the module's permission.
    """
    latest = None
    if report:
        try:
            from core.reporting.site_report import latest as latest_report

            latest = latest_report()
        except Exception as exc:  # noqa: BLE001 - the site still reads
            logger.error("site report not read: %s", type(exc).__name__)
    base = {
        "directory": row.get("directory"),
        "declared": declared(row),
        "revision": row.get("revision"),
        "state": row.get("state") or "none",
        "reason": row.get("reason") or NOTHING_DECLARED,
        "hash": row.get("hash"),
        "applied_at": row.get("applied_at"),
        "rejected": row.get("rejected"),
        "files": row.get("files") or [],
        "report": latest,
    }
    items = row.get("items") or {}
    if not base["directory"]:
        return {**base, "state": "none", "files": [], **empty_items()}
    teams = _active_teams() if items.get("owners") else {}
    owners = [resolve_owner(o, teams) for o in items.get("owners") or []]

    def owned(item: Dict[str, Any]) -> Dict[str, Any]:
        mine = [
            {
                k: o[k]
                for k in ("team", "team_id", "resolved", "unassigned", "file", "line")
                if k in o
            }
            for o in owners
            if any(
                selectors_overlap(a, b)
                for a in item.get("selectors") or []
                for b in o.get("selectors") or []
            )
        ]
        return {**item, "owners": mine}

    return {
        **base,
        "zones": [owned(z) for z in items.get("zones") or []],
        "crown_jewels": [owned(j) for j in items.get("crown_jewels") or []],
        "owners": owners,
        "blind_spots": list(items.get("blind_spots") or []),
        "telemetry": list(items.get("telemetry") or []),
    }


# --- Zone hunts (#5212) ------------------------------------------------------


def resolve_zone(name: str, row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The zone a hunt starts on, as its job request carries it.

    Name, selectors, file, line and revision, and the blind spots a selector of
    which overlaps one of the zone's (``selectors_overlap``). ``ValueError`` names
    why it is refused: no site declared, or no zone by that name.
    """
    row = applied_site(fresh=True) if row is None else row
    if not declared(row):
        raise ValueError(
            f"zone {name!r}: no site is declared, so there is no zone to hunt on"
        )
    items = row.get("items") or {}
    zone = next((z for z in items.get("zones") or [] if z.get("name") == name), None)
    if zone is None:
        known = ", ".join(z["name"] for z in items.get("zones") or []) or "none"
        raise ValueError(
            f"zone {name!r} is not declared in revision {row.get('revision')} "
            f"(declared: {known})"
        )
    selectors = list(zone.get("selectors") or [])
    revision = row.get("revision")
    blind = [
        {
            "what": spot.get("what"),
            "selectors": spot.get("selectors") or [],
            "file": spot.get("file"),
            "line": spot.get("line"),
            "revision": revision,
        }
        for spot in items.get("blind_spots") or []
        if any(
            selectors_overlap(a, b)
            for a in selectors
            for b in spot.get("selectors") or []
        )
    ]
    return {
        "name": zone["name"],
        "selectors": selectors,
        "file": zone.get("file"),
        "line": zone.get("line"),
        "revision": revision,
        "blind_spots": blind,
    }
