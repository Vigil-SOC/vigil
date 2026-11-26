"""The site module (#5213): the declared site against what Wazuh lists.

Four checks: crown jewels heard from, crown jewels in their zone, owners that
resolve to a team, and how many listed hosts no zone covers. Each declared
item is stored with its state (matched, declared but not observed, not
evaluated), its file and line, and the source and time behind it. No agent
name, address or id is stored, logged or put in a reason: agents are turned
into Entity Keys, matched through ``core.site.match``, and dropped.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core import site
from core.memory.entity_keys import normalise_key
from core.reporting.estate import (
    _BUDGET_S,
    _full_access,
    _on,
    _stamp,
    _Stop,
    _stored,
    _timeout,
    _wazuh_call,
    _wazuh_service,
)
from core.time import utcnow

logger = logging.getLogger(__name__)

SEEN = "site.crown_jewel_seen"
PLACE = "site.crown_jewel_place"
OWNER = "site.owner"
UNDECLARED = "site.undeclared"
CHECK_IDS = (SEEN, PLACE, OWNER, UNDECLARED)

PASS = "pass"
FAIL = "fail"
NOT_CHECKED = "not_checked"

MATCHED = "matched"
NOT_OBSERVED = "declared_not_observed"
NOT_EVALUATED = "not_evaluated"

# Past this many agents the read stops and is not checked, never a pass.
MAX_AGENTS = 10_000
_HEARD_WITHIN = timedelta(days=7)
# Selector kinds Wazuh's agent list can match: names and addresses.
_READ_KINDS = frozenset({"host", "ip", "cidr"})
_NAMED = 10  # items a reason names before "and N more"

_CSF = (
    "https://csrc.nist.gov/pubs/cswp/29/the-nist-cybersecurity-framework-csf-20/final"
)
_SP53 = "https://csrc.nist.gov/pubs/sp/800/53/r5/upd1/final"
_CHECKED = "2026-11-26"
_CSF_NAME = "NIST CSF 2.0"
_SP53_NAME = "NIST SP 800-53 Rev. 5"


def _cite(*refs: tuple[str, str, str]) -> dict[str, str]:
    """The existing citation shape. Shown beside the check, never scored."""
    return {
        "source_id": "; ".join(r[0] for r in refs),
        "clause": "; ".join(r[1] for r in refs),
        "link": "; ".join(r[2] for r in refs),
        "date_checked": _CHECKED,
    }


CITATIONS = {
    SEEN: _cite(
        (_CSF_NAME, "ID.AM-01", _CSF),
        (_CSF_NAME, "ID.AM-05", _CSF),
        (_SP53_NAME, "CM-8", _SP53),
    ),
    PLACE: _cite((_CSF_NAME, "ID.AM-05", _CSF), (_SP53_NAME, "CM-8", _SP53)),
    OWNER: _cite((_SP53_NAME, "CM-8(4)", _SP53)),
    UNDECLARED: _cite((_CSF_NAME, "ID.AM-01", _CSF), (_SP53_NAME, "CM-8(3)", _SP53)),
}

# Switched on, and not read by this module yet.
_UNREAD_SOURCES = (("crowdstrike", "CrowdStrike"), ("microsoft-defender", "Defender"))
_NOT_DECLARED = "not checked: no site is declared"
_UNKNOWN_SOURCES = "not checked: could not read which sources are set up"
_WAZUH_OFF = "not checked: Wazuh is not switched on"
_CAPPED = (
    f"not checked: Wazuh lists more than {MAX_AGENTS} agents, "
    "more than this read will page"
)


def _check(
    check_id: str, outcome: str, reason: str, extra: dict[str, Any]
) -> dict[str, Any]:
    return {
        **_stored(check_id, outcome, reason, extra),
        "citation": CITATIONS[check_id],
    }


def produce(sources: Optional[list], health_failed: bool) -> tuple[list, dict]:
    """The module's four checks and its evidence body."""
    stamp = _stamp(utcnow())
    row = site.applied_site(fresh=True)
    if not site.declared(row):
        return (
            [_check(c, NOT_CHECKED, _NOT_DECLARED, {}) for c in CHECK_IDS],
            {"not_checked": _NOT_DECLARED, "checked_at": stamp},
        )
    read_sources: list[dict[str, Any]] = []
    if health_failed or sources is None:
        agents, why = None, _UNKNOWN_SOURCES
    else:
        read_sources = [
            {"source": name, "state": "not_read_yet"}
            for source_id, name in _UNREAD_SOURCES
            if _on(sources, source_id)
        ]
        if _on(sources, "wazuh"):
            agents, why = _read_wazuh()
        else:
            agents, why = None, _WAZUH_OFF
    if why:
        logger.error("day1 site read: %s", why)
    wazuh: dict[str, Any] = {"source": "Wazuh", "checked_at": stamp}
    if agents is None:
        wazuh.update(state=NOT_CHECKED, reason=why)
    else:
        wazuh.update(state="read", listed=len(agents))
    read_sources.insert(0, wazuh)
    return _evaluate(row, agents, why, stamp, read_sources)


# --- The read ----------------------------------------------------------------


def _read_wazuh() -> tuple[Optional[list[dict]], Optional[str]]:
    """Each agent as ``{keys, heard}``, or ``None`` and the not-checked reason."""
    try:
        return asyncio.run(_wazuh_agents()), None
    except _Stop as stop:
        return None, stop.reason
    except Exception:  # noqa: BLE001 - the report still completes
        return None, "not checked: Wazuh could not be read"


async def _wazuh_agents() -> list[dict]:
    service = _wazuh_service()
    deadline = time.monotonic() + _BUDGET_S
    try:
        await _full_access(service, deadline)
        left = max(deadline - time.monotonic(), 0.1)
        try:
            raw, capped = await _wazuh_call(
                asyncio.wait_for(service.read_agents(MAX_AGENTS, "id!=000"), left),
                deadline,
                access=False,
            )
        except asyncio.TimeoutError:
            raise _timeout("Wazuh") from None
    finally:
        await service.close()
    if capped:
        raise _Stop(_CAPPED)
    # The manager is not a host; the query already leaves it out.
    return [_agent(a) for a in raw if str(a.get("id")) != "000"]


def _agent(raw: dict) -> dict:
    keys = []
    if raw.get("name"):
        keys.append(normalise_key(f"host:{raw['name']}"))
    ip = str(raw.get("ip") or "")
    try:
        if not ipaddress.ip_address(ip).is_unspecified:
            keys.append(normalise_key(f"ip:{ip}"))
    except ValueError:
        pass  # "any" until the agent connects
    return {"keys": [k for k in keys if k], "heard": _heard(raw)}


def _heard(raw: dict) -> Optional[datetime]:
    """Last keep-alive, or None for an agent that never connected."""
    if raw.get("status") == "never_connected":
        return None
    try:
        at = datetime.fromisoformat(
            str(raw.get("lastKeepAlive")).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return None if at.year >= 9999 else at


# --- Evaluation --------------------------------------------------------------


def _id(item: dict) -> tuple[str, int]:
    return (item.get("file") or "", int(item.get("line") or 0))


def _where(item: dict) -> str:
    return f"{item.get('file')}:{item.get('line')}"


def _named(items: list[str]) -> str:
    more = len(items) - _NAMED
    text = "; ".join(items[:_NAMED])
    return f"{text}; and {more} more" if more > 0 else text


def _overlaps(a: dict, b: dict) -> bool:
    return any(
        site.selectors_overlap(x, y)
        for x in a.get("selectors") or []
        for y in b.get("selectors") or []
    )


def _unread_reason(item: dict, blind_spots: list[dict]) -> Optional[str]:
    """Why an item is not evaluated even though the read worked."""
    kinds = {s.partition(":")[0] for s in item.get("selectors") or []}
    if not kinds & _READ_KINDS:
        named = ", ".join(f"{k}:" for k in sorted(kinds))
        return f"not checked: no read source lists {named} keys"
    for spot in blind_spots:
        if _overlaps(item, spot):
            return f"not checked: a declared blind spot covers it ({_where(spot)})"
    return None


def _evaluate(
    row: dict,
    agents: Optional[list[dict]],
    why: Optional[str],
    stamp: str,
    read_sources: list[dict],
) -> tuple[list, dict]:
    items = row.get("items") or {}
    zones = items.get("zones") or []
    jewels = items.get("crown_jewels") or []
    spots = items.get("blind_spots") or []
    now = datetime.now(timezone.utc)

    # Which agents (by index) each declared item matches, through the one matcher.
    hits: dict[tuple[str, int], list[int]] = {}
    for index, agent in enumerate(agents or []):
        for found in site.match(agent["keys"], row):
            hits.setdefault(_id(found), [])
            if index not in hits[_id(found)]:
                hits[_id(found)].append(index)

    def heard(index: int) -> Optional[datetime]:
        return agents[index]["heard"] if agents is not None else None

    def seen(index: int) -> bool:
        at = heard(index)
        return at is not None and now - at <= _HEARD_WITHIN

    def state(item: dict, *, spots_apply: bool = True) -> dict[str, Any]:
        reason = why or _unread_reason(item, spots if spots_apply else [])
        if reason:
            return {"state": NOT_EVALUATED, "reason": reason}
        mine = hits.get(_id(item), [])
        body: dict[str, Any] = {
            "state": MATCHED if mine else NOT_OBSERVED,
            "source": "Wazuh",
            "checked_at": stamp,
        }
        times = [t for t in (heard(i) for i in mine) if t is not None]
        if times:
            body["heard_at"] = _stamp(max(times))
        return body

    view = _owner_view(row)
    listed: list[dict[str, Any]] = []
    for zone in zones:
        listed.append(_item("zone", zone["name"], zone, state(zone), view))
    for spot in spots:
        listed.append(
            _item("blind_spot", spot.get("what"), spot, state(spot, spots_apply=False))
        )
    for owner in items.get("owners") or []:
        listed.append(_item("owner", owner.get("team"), owner, state(owner)))

    zone_by_name = {z["name"]: z for z in zones}
    unseen: list[str] = []
    outside: list[str] = []
    seen_count = place_count = 0
    skipped: list[str] = []
    for jewel in jewels:
        body = state(jewel)
        label = f"{jewel['name']} ({_where(jewel)})"
        if body["state"] == NOT_EVALUATED:
            skipped.append(f"{label}: {body['reason'].removeprefix('not checked: ')}")
            body["place"] = NOT_EVALUATED
        else:
            mine = [i for i in hits.get(_id(jewel), []) if seen(i)]
            body["seen"] = bool(mine)
            if not mine:
                unseen.append(label)
                body["place"] = "not checked: not heard from within 7 days"
            else:
                seen_count += 1
                body["place"] = _place(
                    jewel, mine, zone_by_name, hits, agents or [], spots
                )
                if body["place"] == "outside":
                    outside.append(f"{label}, zone {jewel['zone']}")
                if body["place"] in ("inside", "outside"):
                    place_count += 1
        listed.append(_item("crown_jewel", jewel["name"], jewel, body, view))

    checks = [
        _seen_check(jewels, unseen, seen_count, skipped, why, stamp),
        _place_check(jewels, outside, place_count, why, stamp),
        _owner_check(view),
        _undeclared_check(agents, zones, hits, why, stamp),
    ]
    undeclared = checks[-1]["evidence"].get("undeclared")
    if undeclared is not None:
        read_sources[0]["undeclared"] = undeclared
    evidence = {
        "revision": row.get("revision"),
        "checked_at": stamp,
        "sources": read_sources,
        "items": listed,
    }
    return checks, evidence


def _place(
    jewel: dict,
    seen_agents: list[int],
    zones: dict[str, dict],
    hits: dict[tuple[str, int], list[int]],
    agents: list[dict],
    spots: list[dict],
) -> str:
    """``inside``/``outside``, or the reason place is not checked."""
    zone = zones.get(jewel.get("zone") or "")
    if zone is None:
        return "not checked: names no zone"
    if _unread_reason(zone, spots):
        return "not checked: its zone is not evaluated"
    kinds = {
        "ip" if s.startswith("cidr:") else s.partition(":")[0]
        for s in zone.get("selectors") or []
    }
    placeable = [
        i
        for i in seen_agents
        if kinds & {k.partition(":")[0] for k in agents[i]["keys"]}
    ]
    if not placeable:
        return "not checked: the zone's selectors are of a kind its host does not list"
    inside = set(hits.get(_id(zone), []))
    return "outside" if any(i not in inside for i in placeable) else "inside"


def _owner_view(row: dict) -> Optional[dict]:
    """``site_view``'s owner resolution, or None when teams could not be read."""
    try:
        return site.site_view(row, report=False, proposal=False)
    except Exception as exc:  # noqa: BLE001 - this check only
        logger.error("day1 site check %s: %s", OWNER, type(exc).__name__)
        return None


def _owners_of(item: dict, view: Optional[dict]) -> list[dict]:
    if view is None:
        return []
    for listed in (view.get("zones") or []) + (view.get("crown_jewels") or []):
        if _id(listed) == _id(item):
            return listed.get("owners") or []
    return []


def _item(
    kind: str, name: Any, item: dict, body: dict, view: Optional[dict] = None
) -> dict[str, Any]:
    out = {
        "kind": kind,
        "name": name,
        "file": item.get("file"),
        "line": item.get("line"),
    }
    out.update(body)
    if kind in ("zone", "crown_jewel") and view is not None:
        teams = [o["resolved"] for o in _owners_of(item, view) if o.get("resolved")]
        out["owners"] = teams
    return out


def _seen_check(jewels, unseen, seen_count, skipped, why, stamp) -> dict:
    if not jewels:
        return _check(SEEN, NOT_CHECKED, "not checked: no crown jewel is declared", {})
    if why:
        return _check(SEEN, NOT_CHECKED, why, {"checked_at": stamp})
    evidence = {
        "checked": seen_count + len(unseen),
        "unseen": len(unseen),
        "checked_at": stamp,
    }
    tail = f" Not checked: {_named(skipped)}." if skipped else ""
    if unseen:
        return _check(
            SEEN,
            FAIL,
            f"Crown jewels not heard from within 7 days: {len(unseen)} of "
            f"{evidence['checked']}: {_named(unseen)}. Sources read: Wazuh.{tail}",
            evidence,
        )
    if not seen_count:
        return _check(SEEN, NOT_CHECKED, f"not checked: {_named(skipped)}", evidence)
    return _check(
        SEEN,
        PASS,
        f"Crown jewels heard from within 7 days: {seen_count} of {seen_count}. "
        f"Sources read: Wazuh.{tail}",
        evidence,
    )


def _place_check(jewels, outside, place_count, why, stamp) -> dict:
    if not jewels:
        return _check(PLACE, NOT_CHECKED, "not checked: no crown jewel is declared", {})
    if why:
        return _check(PLACE, NOT_CHECKED, why, {"checked_at": stamp})
    evidence = {"checked": place_count, "outside": len(outside), "checked_at": stamp}
    if outside:
        return _check(
            PLACE,
            FAIL,
            f"Seen crown jewels with a host outside their zone: {len(outside)} of "
            f"{place_count}: {_named(outside)}.",
            evidence,
        )
    if not place_count:
        return _check(
            PLACE,
            NOT_CHECKED,
            "not checked: no crown jewel was both heard from within 7 days and "
            "placeable in a zone it names",
            evidence,
        )
    return _check(
        PLACE,
        PASS,
        f"Seen crown jewels in the zone they name: {place_count} of {place_count}.",
        evidence,
    )


def _owner_check(view: Optional[dict]) -> dict:
    if view is None:
        return _check(OWNER, NOT_CHECKED, "not checked: could not read teams", {})
    owned = [("zone", z) for z in view.get("zones") or []] + [
        ("crown jewel", j) for j in view.get("crown_jewels") or []
    ]
    if not owned:
        return _check(
            OWNER, NOT_CHECKED, "not checked: no zone or crown jewel is declared", {}
        )
    unowned = []
    for kind, item in owned:
        owners = item.get("owners") or []
        if any(o.get("resolved") for o in owners):
            continue
        why = "; ".join(o["unassigned"] for o in owners if o.get("unassigned"))
        unowned.append(
            f"{kind} {item['name']} ({_where(item)}): {why or 'no owner is declared'}"
        )
    evidence = {"checked": len(owned), "unowned": len(unowned)}
    if unowned:
        return _check(
            OWNER,
            FAIL,
            f"Zones and crown jewels with no active team: {len(unowned)} of "
            f"{len(owned)}: {_named(unowned)}.",
            evidence,
        )
    return _check(
        OWNER,
        PASS,
        f"Zones and crown jewels with an active team: {len(owned)} of {len(owned)}.",
        evidence,
    )


def _undeclared_check(agents, zones, hits, why, stamp) -> dict:
    if why:
        return _check(UNDECLARED, NOT_CHECKED, why, {"checked_at": stamp})
    if not agents:
        return _check(
            UNDECLARED,
            NOT_CHECKED,
            "not checked: Wazuh lists no agents",
            {"listed": 0, "checked_at": stamp},
        )
    zoned = {i for z in zones for i in hits.get(_id(z), [])}
    count = sum(1 for i in range(len(agents)) if i not in zoned)
    evidence = {"listed": len(agents), "undeclared": count, "checked_at": stamp}
    if count:
        return _check(
            UNDECLARED,
            FAIL,
            f"Wazuh agents in no declared zone: {count} of {len(agents)}. "
            "The manager is not counted.",
            evidence,
        )
    return _check(
        UNDECLARED,
        PASS,
        f"Wazuh agents in no declared zone: 0 of {len(agents)}. "
        "The manager is not counted.",
        evidence,
    )


# --- Read surface ------------------------------------------------------------


def latest() -> Optional[dict[str, Any]]:
    """The latest Day 1 record's site checks and evidence, for ``site_view``."""
    from core.reporting.day1 import DAY1_TRIGGERS
    from core.storage.models import ReportRecord
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        for record in (
            session.query(ReportRecord)
            .filter(ReportRecord.trigger.in_(DAY1_TRIGGERS))
            .order_by(ReportRecord.recorded_at.desc())
            .limit(20)
        ):
            if "site" not in (record.modules or []):
                continue
            return {
                "record_id": record.record_id,
                "recorded_at": _stamp(record.recorded_at),
                "checks": [
                    {
                        k: check.get(k)
                        for k in ("id", "outcome", "reason", "evidence", "citation")
                    }
                    for check in record.checks or []
                    if check.get("module") == "site"
                ],
                "evidence": (record.evidence or {}).get("site"),
            }
    return None
