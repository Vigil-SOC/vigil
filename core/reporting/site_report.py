"""The site module (#5213): the declared site against what Wazuh, CrowdStrike
and Defender list.

Four checks: crown jewels heard from, crown jewels in their zone, owners that
resolve to a team, and how many listed hosts no zone covers. Each declared
item is stored with its state (matched, declared but not observed, not
evaluated), its file and line, and the source and time behind it. No agent
name, address or id is stored, logged or put in a reason: hosts are turned
into Entity Keys, matched through ``core.site.match``, and dropped. Each
switched-on source is read on its own; the checks are not checked only when
none could be read.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core import site
from core.integrations._base.config import missing, resolve
from core.integrations.crowdstrike.client import CrowdStrikeService
from core.integrations.crowdstrike.descriptor import CROWDSTRIKE
from core.integrations.microsoft_defender.ingestion import MicrosoftDefenderIngestion
from core.memory.entity_keys import normalise_key
from core.reporting.estate import (
    _BUDGET_S,
    ONBOARDED,
    _def_pages,
    _full_access,
    _on,
    _past,
    _stamp,
    _Stop,
    _stop_for,
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

# Past this many hosts in one source its read stops and is not checked.
MAX_AGENTS = 10_000
_HEARD_WITHIN = timedelta(days=7)
# Selector kinds the sources' host lists can match: names and addresses.
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

# (source id, name, the noun its estate checks use for a host), in read order.
_SOURCES = (
    ("wazuh", "Wazuh", "agents"),
    ("crowdstrike", "CrowdStrike", "endpoints"),
    ("microsoft-defender", "Defender", "endpoints"),
)
_NOUN = {name: noun for _sid, name, noun in _SOURCES}
_NOT_DECLARED = "not checked: no site is declared"
_UNKNOWN_SOURCES = "not checked: could not read which sources are set up"
_NONE_ON = "not checked: none of Wazuh, CrowdStrike or Defender is switched on"


def _capped(name: str) -> str:
    return (
        f"not checked: {name} lists more than {MAX_AGENTS} {_NOUN[name]}, "
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
    hosts: list[dict] = []
    if health_failed or sources is None:
        why: Optional[str] = _UNKNOWN_SOURCES
    else:
        # Each source is read on its own; one failing does not stop the others.
        for source_id, name, _noun in _SOURCES:
            if not _on(sources, source_id):
                continue
            listed, reason = _READS[name]()
            entry: dict[str, Any] = {"source": name, "checked_at": stamp}
            if listed is None:
                logger.error("day1 site read %s: %s", name, reason)
                entry.update(state=NOT_CHECKED, reason=reason)
            else:
                entry.update(state="read", listed=len(listed))
                hosts.extend({**h, "source": name} for h in listed)
            read_sources.append(entry)
        why = _no_source(read_sources)
    if why == _UNKNOWN_SOURCES or why == _NONE_ON:
        logger.error("day1 site read: %s", why)
    return _evaluate(row, hosts, why, stamp, read_sources)


def _no_source(read_sources: list[dict]) -> Optional[str]:
    """The checks' not-checked reason when no switched-on source was read."""
    if not read_sources:
        return _NONE_ON
    if any(s["state"] == "read" for s in read_sources):
        return None
    if len(read_sources) == 1:
        return read_sources[0]["reason"]
    reasons = "; ".join(
        f"{s['source']}: {s['reason'].removeprefix('not checked: ')}"
        for s in read_sources
    )
    return f"not checked: no switched-on source could be read. {reasons}"


# --- The reads ---------------------------------------------------------------
# Each returns hosts as ``{keys, heard}``, or ``None`` and the not-checked
# reason. Names and addresses become Entity Keys at once and are not kept.


def _guarded(name: str, read) -> tuple[Optional[list[dict]], Optional[str]]:
    try:
        return read(), None
    except _Stop as stop:
        return None, stop.reason
    except Exception:  # noqa: BLE001 - the report still completes
        return None, f"not checked: {name} could not be read"


def _read_wazuh() -> tuple[Optional[list[dict]], Optional[str]]:
    return _guarded("Wazuh", lambda: asyncio.run(_wazuh_agents()))


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
        raise _Stop(_capped("Wazuh"))
    # The manager is not a host; the query already leaves it out.
    return [_agent(a) for a in raw if str(a.get("id")) != "000"]


def _agent(raw: dict) -> dict:
    heard = None if raw.get("status") == "never_connected" else raw.get("lastKeepAlive")
    return _host([raw.get("name")], raw.get("ip"), heard)


def _read_crowdstrike() -> tuple[Optional[list[dict]], Optional[str]]:
    return _guarded("CrowdStrike", _falcon_hosts)


def _falcon_hosts() -> list[dict]:
    config = resolve(CROWDSTRIKE)
    if missing(config, "client_id", "client_secret"):
        raise _Stop("not checked: CrowdStrike could not be read")
    service = CrowdStrikeService(
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        base_url=config.get("base_url") or "https://api.crowdstrike.com",
        timeout=_BUDGET_S,
    )
    deadline = time.monotonic() + _BUDGET_S
    hosts: list[dict] = []
    offset: Optional[str] = None
    try:
        while True:
            _past(deadline, "CrowdStrike")
            service.timeout = max(deadline - time.monotonic(), 0.1)
            page = service.device_hosts(offset)
            if page["kind"] != "ok":
                if time.monotonic() >= deadline:
                    raise _timeout("CrowdStrike")
                raise _stop_for("CrowdStrike", page["kind"], page["status"])
            if page["total"] > MAX_AGENTS:
                raise _Stop(_capped("CrowdStrike"))
            hosts.extend(
                _host([h["hostname"]], h["local_ip"], h["last_seen"])
                for h in page["hosts"]
            )
            offset = page["next"]
            if not offset or not page["hosts"] or len(hosts) >= page["total"]:
                return hosts
    finally:
        service.session.close()


def _read_defender() -> tuple[Optional[list[dict]], Optional[str]]:
    return _guarded("Defender", _defender_hosts)


def _defender_hosts() -> list[dict]:
    ingestion = MicrosoftDefenderIngestion()
    hosts: list[dict] = []

    def page(odata_filter: str, skip: int, timeout: float) -> tuple[int, int]:
        rows = ingestion.host_machine_page(odata_filter, skip, timeout)
        hosts.extend(_machine(r) for r in rows if isinstance(r, dict))
        if len(hosts) > MAX_AGENTS:
            raise _Stop(_capped("Defender"))
        return len(rows), len(rows)

    deadline = time.monotonic() + _BUDGET_S
    _def_pages(page, ONBOARDED, deadline, [0], first=True)
    return hosts


def _machine(raw: dict) -> dict:
    """``computerDnsName`` is fully qualified; the first label is minted too so a
    declared short ``host:`` name matches, as it does for Wazuh and Falcon."""
    name = str(raw.get("computerDnsName") or "")
    return _host(
        [name, name.partition(".")[0]], raw.get("lastIpAddress"), raw.get("lastSeen")
    )


_READS = {
    "Wazuh": _read_wazuh,
    "CrowdStrike": _read_crowdstrike,
    "Defender": _read_defender,
}


def _host(names: list, ip: Any, heard: Any) -> dict:
    """One host as Entity Keys and a last-heard time; nothing else is kept."""
    keys = [normalise_key(f"host:{n}") for n in names if n]
    try:
        if not ipaddress.ip_address(str(ip or "")).is_unspecified:
            keys.append(normalise_key(f"ip:{ip}"))
    except ValueError:
        pass  # Wazuh's "any" until the agent connects, or no address
    return {"keys": list(dict.fromkeys(k for k in keys if k)), "heard": _at(heard)}


def _at(value: Any) -> Optional[datetime]:
    """A last-seen time, or None for never heard or unreadable."""
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
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
    hosts: list[dict],
    why: Optional[str],
    stamp: str,
    read_sources: list[dict],
) -> tuple[list, dict]:
    """Hosts are ``{source, keys, heard}`` from every source read, matched
    through the one matcher; ``why`` is set only when no source was read."""
    items = row.get("items") or {}
    zones = items.get("zones") or []
    jewels = items.get("crown_jewels") or []
    spots = items.get("blind_spots") or []
    now = datetime.now(timezone.utc)
    read = [s["source"] for s in read_sources if s["state"] == "read"]
    unread = [s["source"] for s in read_sources if s["state"] != "read"]
    sources_line = f" Sources read: {', '.join(read)}." + (
        f" Not read: {', '.join(unread)}." if unread else ""
    )

    # Which hosts (by index) each declared item matches.
    hits: dict[tuple[str, int], list[int]] = {}
    for index, host in enumerate(hosts):
        for found in site.match(host["keys"], row):
            mine = hits.setdefault(_id(found), [])
            if index not in mine:
                mine.append(index)

    def seen(index: int) -> bool:
        at = hosts[index]["heard"]
        return at is not None and now - at <= _HEARD_WITHIN

    def state(item: dict, *, spots_apply: bool = True) -> dict[str, Any]:
        reason = why or _unread_reason(item, spots if spots_apply else [])
        if reason:
            return {"state": NOT_EVALUATED, "reason": reason}
        mine = hits.get(_id(item), [])
        behind = list(dict.fromkeys(hosts[i]["source"] for i in mine)) or read
        body: dict[str, Any] = {
            "state": MATCHED if mine else NOT_OBSERVED,
            "source": ", ".join(behind),
            "checked_at": stamp,
        }
        times = [t for t in (hosts[i]["heard"] for i in mine) if t is not None]
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
                body["place"] = _place(jewel, mine, zone_by_name, hits, hosts, spots)
                if body["place"] == "outside":
                    outside.append(f"{label}, zone {jewel['zone']}")
                if body["place"] in ("inside", "outside"):
                    place_count += 1
        listed.append(_item("crown_jewel", jewel["name"], jewel, body, view))

    # Per source: hosts it lists that no declared zone matches.
    zoned = {i for z in zones for i in hits.get(_id(z), [])}
    for entry in read_sources:
        if entry["state"] == "read":
            entry["undeclared"] = sum(
                1
                for i, h in enumerate(hosts)
                if h["source"] == entry["source"] and i not in zoned
            )

    checks = [
        _seen_check(jewels, unseen, seen_count, skipped, why, stamp, sources_line),
        _place_check(jewels, outside, place_count, why, stamp, sources_line),
        _owner_check(view),
        _undeclared_check(read_sources, why, stamp),
    ]
    evidence = {
        "revision": row.get("revision"),
        "checked_at": stamp,
        "sources": read_sources,
        "items": listed,
    }
    return checks, evidence


def _place(
    jewel: dict,
    seen_hosts: list[int],
    zones: dict[str, dict],
    hits: dict[tuple[str, int], list[int]],
    hosts: list[dict],
    spots: list[dict],
) -> str:
    """``inside``/``outside`` from whichever sources heard the jewel's host,
    or the reason place is not checked."""
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
        i for i in seen_hosts if kinds & {k.partition(":")[0] for k in hosts[i]["keys"]}
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


def _seen_check(jewels, unseen, seen_count, skipped, why, stamp, sources) -> dict:
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
            f"{evidence['checked']}: {_named(unseen)}.{sources}{tail}",
            evidence,
        )
    if not seen_count:
        return _check(SEEN, NOT_CHECKED, f"not checked: {_named(skipped)}", evidence)
    return _check(
        SEEN,
        PASS,
        f"Crown jewels heard from within 7 days: {seen_count} of {seen_count}."
        f"{sources}{tail}",
        evidence,
    )


def _place_check(jewels, outside, place_count, why, stamp, sources) -> dict:
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
            f"{place_count}: {_named(outside)}.{sources}",
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
        f"Seen crown jewels in the zone they name: {place_count} of {place_count}."
        f"{sources}",
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


def _undeclared_check(read_sources: list[dict], why, stamp) -> dict:
    """Counts per source read, never the hosts."""
    if why:
        return _check(UNDECLARED, NOT_CHECKED, why, {"checked_at": stamp})
    read = [s for s in read_sources if s["state"] == "read"]
    listed = sum(s["listed"] for s in read)
    if not listed:
        names = " and ".join(s["source"] for s in read)
        nouns = " or ".join(dict.fromkeys(_NOUN[s["source"]] for s in read))
        return _check(
            UNDECLARED,
            NOT_CHECKED,
            f"not checked: {names} list{'s' if len(read) == 1 else ''} no {nouns}",
            {"listed": 0, "checked_at": stamp},
        )
    count = sum(s["undeclared"] for s in read)
    evidence = {
        "listed": listed,
        "undeclared": count,
        "by_source": {
            s["source"]: {"listed": s["listed"], "undeclared": s["undeclared"]}
            for s in read
        },
        "checked_at": stamp,
    }
    counts = "; ".join(
        f"{s['source']}: {s['undeclared']} of {s['listed']} {_NOUN[s['source']]}"
        for s in read
    )
    manager = (
        " The Wazuh manager is not counted."
        if any(s["source"] == "Wazuh" for s in read)
        else ""
    )
    return _check(
        UNDECLARED,
        FAIL if count else PASS,
        f"Hosts in no declared zone: {counts}.{manager}",
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
