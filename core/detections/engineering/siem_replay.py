"""Replay a proposal's Sigma rule on the SIEM's own events (#4157, step 2 of 6).

1.x replayed a proposal on Vigil's ``log_events``; main keeps no log store, so the
events come from each SIEM Vigil reads, over the replay window (30 days by default;
a SIEM holds what its retention keeps):

- Elastic: the elastic-siem cluster's event indices (``ELASTIC_INDICES`` unless the
  caller names others);
- Wazuh: its archive (``wazuh-archives-*``) on the indexer behind elastic-siem;
- Splunk: the splunk integration (``index=*`` unless the caller names one).

Per SIEM, two reads, each one search of that SIEM's budget:

- **attack**: the events a person named by id, else the events of the proposal's
  Case: on its hosts, users or addresses, inside its window (its Verdict's, else
  its Findings'), padded by ``CLAIM_PAD``; the newest ``MAX_ATTACK``;
- **benign**: the newest ``MAX_BENIGN`` events in the window that hold a field the
  rule reads. Those any Case or hunt claims (a Verdict's hosts, users or
  addresses inside its window, unless the Verdict ruled the run out) are left
  out, and so are the proposal's own Case's events, but only inside that Case's
  padded window: its host's events outside it count as normal. What remains is
  taken as normal activity, so a false positive here may be an attack nobody
  linked, and the evidence says so.

The rule is matched with main's own Sigma evaluator (``core.detections.replay``).
A rule's field is looked up as written, else by the last part of the event's own
field path when only one field ends that way (``Image`` finds
``winlog.event_data.Image`` and Wazuh's ``data.win.eventdata.image``).

A replay **proves** the rule only when it hit at least one attack event, matched no
benign event, at least one benign event held a field the rule reads, and no read
was cut short, deferred or failed. Otherwise ``unproven_because`` says why, in
plain words. Nothing here writes: the step API (c) stores the evidence.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import yaml

from core.detections import lint as lint_mod
from core.detections.replay import _compile_rule, _Unevaluable

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
MAX_ATTACK = 2000
MAX_BENIGN = 5000
SAMPLES = 20
# A Verdict's or Finding's window, widened: an event is indexed a little after,
# and a Finding stamps a little before, what it describes.
CLAIM_PAD = timedelta(hours=1)
ELASTIC_INDICES = "logs-*,winlogbeat-*,filebeat-*"
WAZUH_ARCHIVES = "wazuh-archives-*"
SPLUNK_INDEX = "*"

# Fields that name the machine, the account or the address an event is about.
_HOST_PATHS = frozenset({"host.name", "host.hostname", "agent.name"})
_USER_PATHS = frozenset({"user.name", "user.id"})
_IP_STEMS = frozenset(
    {
        "ip",
        "srcip",
        "dstip",
        "destip",
        "sourceip",
        "destinationip",
        "clientip",
        "srcaddr",
        "dstaddr",
        "ipaddress",
    }
)


# --- events -----------------------------------------------------------------------


def _stem(path: str) -> str:
    return path.split(".")[-1].lower().replace("-", "").replace("_", "")


def flatten(record: Mapping[str, Any], prefix: str = "") -> Dict[str, Any]:
    """Dotted keys for nested maps; a key already dotted stays as it is."""
    out: Dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            out.update(flatten(value, f"{name}."))
        else:
            out[name] = value
    return out


def _tail(name: str) -> str:
    """A field's last part, as rules and SIEMs spell it alike: Sigma's
    ``EventID`` is winlogbeat's ``event_id`` and Wazuh's ``eventID``."""
    return name.rsplit(".", 1)[-1].lower().replace("_", "").replace("-", "")


class View(dict):
    """An event as a rule sees it: its dotted fields, plus each field's last part
    (case, ``_`` and ``-`` aside) when no field ending the same way holds another
    value."""

    def __init__(self, flat: Mapping[str, Any]):
        super().__init__(flat)
        tails: Dict[str, List[str]] = {}
        for path in flat:
            tails.setdefault(_tail(path), []).append(path)
        self._alias = {
            t: paths[0]
            for t, paths in tails.items()
            if len({str(flat[p]) for p in paths}) == 1
        }

    def _path(self, key: Any) -> Optional[str]:
        if dict.__contains__(self, key):
            return key
        if isinstance(key, str) and "." not in key:
            return self._alias.get(_tail(key))
        return None

    def __contains__(self, key: Any) -> bool:
        return self._path(key) is not None

    def __getitem__(self, key: Any) -> Any:
        path = self._path(key)
        if path is None:
            raise KeyError(key)
        return dict.__getitem__(self, path)

    def get(self, key: Any, default: Any = None) -> Any:
        path = self._path(key)
        return default if path is None else dict.__getitem__(self, path)


def _host(value: str) -> Set[str]:
    full = value.strip().lower()
    return {full, full.split(".", 1)[0]} if full else set()


def _user(value: str) -> Set[str]:
    name = value.strip().lower().split("\\")[-1].split("@")[0]
    return {name} if name else set()


@dataclass
class Event:
    siem: str
    id: str
    at: Optional[datetime]
    view: View
    hosts: Set[str] = field(default_factory=set)
    users: Set[str] = field(default_factory=set)
    ips: Set[str] = field(default_factory=set)

    def sample(self, fields: Sequence[str]) -> Dict[str, Any]:
        shown = {f: self.view.get(f) for f in fields if f in self.view}
        return {
            "siem": self.siem,
            "id": self.id,
            "at": self.at.isoformat() if self.at else None,
            "host": sorted(self.hosts)[0] if self.hosts else None,
            "fields": shown,
        }


def _when(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def event_of(siem: str, hit: Mapping[str, Any]) -> Event:
    """An Elasticsearch hit (or a Splunk row shaped as one) as an Event."""
    source = hit.get("_source") if isinstance(hit.get("_source"), Mapping) else hit
    flat = flatten(source or {})
    view = View(flat)
    event = Event(
        siem=siem,
        id=str(hit.get("_id") or flat.get("_cd") or ""),
        at=_when(flat.get("@timestamp") or flat.get("_time") or flat.get("timestamp")),
        view=view,
    )
    for path, value in flat.items():
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            continue
        text = str(value)
        stem = _stem(path)
        if path in _HOST_PATHS or stem in lint_mod._HOST_FIELDS:
            event.hosts |= _host(text)
        if path in _USER_PATHS or stem in lint_mod._USER_FIELDS:
            event.users |= _user(text)
        if stem in _IP_STEMS or path.endswith(".ip"):
            event.ips.add(text.strip())
    return event


# --- claims -----------------------------------------------------------------------


@dataclass
class Claim:
    """What a Case or hunt holds as its own: events on these hosts, users or
    addresses, inside this window."""

    ref: str
    start: datetime
    end: datetime
    hosts: Set[str] = field(default_factory=set)
    users: Set[str] = field(default_factory=set)
    ips: Set[str] = field(default_factory=set)
    ruled_out: bool = False

    def holds(self, event: Event) -> bool:
        if event.at is None or not (self.start <= event.at <= self.end):
            return False
        return bool(
            self.hosts & event.hosts or self.users & event.users or self.ips & event.ips
        )

    def empty(self) -> bool:
        return not (self.hosts or self.users or self.ips)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def claim_of(
    ref: str,
    entities: Mapping[str, Iterable[str]],
    start: datetime,
    end: datetime,
    ruled_out: bool = False,
) -> Claim:
    claim = Claim(
        ref=ref,
        start=_utc(start) - CLAIM_PAD,
        end=_utc(end) + CLAIM_PAD,
        ruled_out=ruled_out,
    )
    for host in entities.get("hostnames", ()):
        claim.hosts |= _host(str(host))
    for user in entities.get("users", ()):
        claim.users |= _user(str(user))
    claim.ips |= {str(ip).strip() for ip in entities.get("src_ips", ()) if ip}
    return claim


def ledger_claims(since_days: int) -> List[Claim]:
    """Every Case and hunt Verdict concluded in the window, as a claim."""
    from core.detections.gaps import RULED_OUT, read_ledger

    return [
        claim_of(
            f"{v['investigation_kind']}:{v['investigation_id']}",
            v.get("entities") or {},
            v["first_seen"],
            v["last_seen"],
            ruled_out=v["outcome"] in RULED_OUT,
        )
        for v in read_ledger(since_days)
        if v.get("first_seen") and v.get("last_seen")
    ]


def case_claim(case_id: str, claims: Sequence[Claim]) -> Optional[Claim]:
    """The Case's own claim: its Verdict's, else its Findings' entities over their
    time span. None when the Case names no host, user or address."""
    for claim in claims:
        if claim.ref == f"case:{case_id}" and not claim.empty():
            return claim
    from core.detections.reconstruction import _entities_from_context
    from core.storage.connection import get_db_manager
    from core.storage.models import Finding
    from core.storage.models.base import case_findings

    entities: Dict[str, List[str]] = {"hostnames": [], "users": [], "src_ips": []}
    times: List[datetime] = []
    with get_db_manager().session_scope() as session:
        rows = (
            session.query(
                Finding.entity_context, Finding.timestamp, Finding.ip_addresses
            )
            .join(case_findings, case_findings.c.finding_id == Finding.finding_id)
            .filter(case_findings.c.case_id == case_id)
            .all()
        )
    for context, at, addresses in rows:
        if isinstance(context, dict):
            for key, values in _entities_from_context(context).items():
                entities.setdefault(key, []).extend(values)
        entities["src_ips"].extend(addresses or [])
        if at is not None:
            times.append(_utc(at))
    if not times:
        return None
    claim = claim_of(f"case:{case_id}", entities, min(times), max(times))
    return None if claim.empty() else claim


# --- the rule ---------------------------------------------------------------------


def rule_fields(sigma: str) -> List[str]:
    """The fields the rule's detection reads, modifiers dropped, in order."""
    try:
        doc = yaml.safe_load(sigma) or {}
    except yaml.YAMLError:
        return []
    detection = doc.get("detection") if isinstance(doc, dict) else None
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                name = str(key).split("|", 1)[0]
                if name and name not in found:
                    found.append(name)
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    walk(item)

    if isinstance(detection, dict):
        for name, selection in detection.items():
            if name not in ("condition", "timeframe"):
                walk(selection)
    return found


def _variants(name: str) -> List[str]:
    lower_first = name[:1].lower() + name[1:]
    return sorted({name, lower_first, name.lower()})


def _snake(name: str) -> str:
    """``EventID`` as winlogbeat spells it, ``event_id``; ``CommandLine`` as
    ``command_line``."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


# --- reading the SIEMs ------------------------------------------------------------


@dataclass
class Read:
    total: int = 0
    docs: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None
    deferred: bool = False

    @property
    def cut_short(self) -> bool:
        return self.total > len(self.docs)


class Source:
    """One SIEM's reads. ``spend`` takes one search of its budget, or says no."""

    siem = ""

    def __init__(self, service: Any, index: str, per_hour: int):
        self.service, self.index, self.per_hour = service, index, per_hour

    def spend(self, what: str) -> bool:  # pragma: no cover - each SIEM's own
        raise NotImplementedError

    async def attack(
        self,
        claim: Optional[Claim],
        ids: Sequence[str],
        fields: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> Read:
        raise NotImplementedError

    async def benign(
        self, fields: Sequence[str], start: datetime, end: datetime
    ) -> Read:
        raise NotImplementedError

    async def case_events(
        self, claim: Claim, start: datetime, end: datetime, size: int
    ) -> Read:
        """The newest of the Case's events, whatever fields they hold: what a rule
        drafted from the Case gets to see."""
        raise NotImplementedError

    async def close(self) -> None:
        close = getattr(self.service, "close", None)
        done = close() if close is not None else None
        if asyncio.iscoroutine(done):
            await done


def _holding(fields: Sequence[str]) -> Dict[str, Any]:
    """Events where a field the rule reads is present, under any parent path."""
    exists = [
        {"exists": {"field": path}}
        for name in fields
        for variant in _variants(name) + [_snake(name), _snake(name).replace("_", "-")]
        for path in (variant, f"*.{variant}")
    ]
    return {"bool": {"should": exists, "minimum_should_match": 1}}


def _entity_should(claim: Claim) -> List[Dict[str, Any]]:
    should: List[Dict[str, Any]] = []
    for host in claim.hosts:
        for path in (
            "host.name",
            "host.hostname",
            "agent.name",
            "winlog.computer_name",
        ):
            should.append({"term": {path: host}})
    for user in claim.users:
        should.append({"term": {"user.name": user}})
    for ip in claim.ips:
        for path in ("source.ip", "destination.ip", "host.ip", "data.srcip"):
            should.append({"term": {path: ip}})
    return should


class ElasticSource(Source):
    siem = "elastic"

    def __init__(self, service: Any, index: str, per_hour: int, integration: str):
        super().__init__(service, index, per_hour)
        self.integration = integration

    def spend(self, what: str) -> bool:
        from core.integrations._base import search_budget

        return search_budget.spend(
            self.integration, self.per_hour, f"proposal-replay:{self.siem}:{what}"
        )

    async def _search(
        self, query: Dict[str, Any], start: datetime, end: datetime, size: int
    ) -> Read:
        window = {"@timestamp": {"gte": start.isoformat(), "lte": end.isoformat()}}
        # Newest first: unsorted, a busy host's first page is any of its events.
        total, docs = await self.service.dsl_search(
            self.index, query, window, size, sort=NEWEST_FIRST
        )
        return Read(total=total, docs=docs)

    async def attack(
        self,
        claim: Optional[Claim],
        ids: Sequence[str],
        fields: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> Read:
        if ids:
            query: Dict[str, Any] = {"ids": {"values": list(ids)}}
        elif claim is not None:
            # Only the Case's events a rule field is on: a busy host holds many.
            start, end = max(start, claim.start), min(end, claim.end)
            query = {"bool": {"filter": [_entity(claim), _holding(fields)]}}
        else:
            return Read()
        return await self._search(query, start, end, MAX_ATTACK)

    async def benign(
        self, fields: Sequence[str], start: datetime, end: datetime
    ) -> Read:
        return await self._search(_holding(fields), start, end, MAX_BENIGN)

    async def case_events(
        self, claim: Claim, start: datetime, end: datetime, size: int
    ) -> Read:
        return await self._search(
            _entity(claim), max(start, claim.start), min(end, claim.end), size
        )


def _entity(claim: Claim) -> Dict[str, Any]:
    return {"bool": {"should": _entity_should(claim), "minimum_should_match": 1}}


NEWEST_FIRST = [{"@timestamp": {"order": "desc", "unmapped_type": "date"}}]


class WazuhSource(ElasticSource):
    siem = "wazuh"


def _spl_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _spl_holding(fields: Sequence[str]) -> str:
    held = " OR ".join(f"{v}=*" for name in fields for v in _variants(name))
    return f" ({held})" if held else ""


class SplunkSource(Source):
    siem = "splunk"

    def spend(self, what: str) -> bool:
        from core.integrations._base import search_budget
        from core.integrations.splunk.descriptor import SPLUNK

        return search_budget.spend(
            SPLUNK.id, self.per_hour, f"proposal-replay:{self.siem}:{what}"
        )

    async def _search(
        self, spl: str, start: datetime, end: datetime, size: int
    ) -> Read:
        from core.detections.inventory.rollout import _splunk_doc

        # Splunk returns events newest first. The table keeps every row and field;
        # a verbose-mode (all_fields) job may keep fewer results than asked for.
        found = await asyncio.to_thread(
            self.service.search,
            f"{spl} | head {size} | table _bkt _cd _time host sourcetype *",
            earliest_time=f"{start.timestamp():.3f}",
            latest_time=f"{end.timestamp():.3f}",
            max_count=size,
        )
        rows = list(found.results)[:size]
        # Splunk's event count is what it scanned, not what matched: a full page
        # is the sign more remain.
        total = len(rows) + 1 if len(rows) >= size else len(rows)
        return Read(total=total, docs=[_splunk_doc(r) for r in rows])

    async def attack(
        self,
        claim: Optional[Claim],
        ids: Sequence[str],
        fields: Sequence[str],
        start: datetime,
        end: datetime,
    ) -> Read:
        if ids:
            # An id is _bkt:_cd, as _splunk_doc writes it, or _cd alone.
            named = " OR ".join(
                f"_cd={_spl_value(i)} OR _named={_spl_value(i)}" for i in ids
            )
            spl = (
                f"search index={self.index}"
                f' | eval _named=_bkt.":"._cd | where {named}'
            )
            return await self._search(spl, start, end, MAX_ATTACK)
        if claim is None:
            return Read()
        terms = _claim_terms(claim)
        if not terms:
            return Read()
        spl = f"search index={self.index} ({terms}){_spl_holding(fields)}"
        return await self._search(
            spl, max(start, claim.start), min(end, claim.end), MAX_ATTACK
        )

    async def benign(
        self, fields: Sequence[str], start: datetime, end: datetime
    ) -> Read:
        return await self._search(
            f"search index={self.index}{_spl_holding(fields)}", start, end, MAX_BENIGN
        )

    async def case_events(
        self, claim: Claim, start: datetime, end: datetime, size: int
    ) -> Read:
        terms = _claim_terms(claim)
        if not terms:
            return Read()
        return await self._search(
            f"search index={self.index} ({terms})",
            max(start, claim.start),
            min(end, claim.end),
            size,
        )


def _claim_terms(claim: Claim) -> str:
    terms = [f"host={_spl_value(h)}" for h in claim.hosts]
    terms += [f"Computer={_spl_value(h)}" for h in claim.hosts]
    terms += [f"user={_spl_value(u)}" for u in claim.users]
    terms += [f"src_ip={_spl_value(i)} OR dest_ip={_spl_value(i)}" for i in claim.ips]
    return " OR ".join(terms)


def configured_sources(
    elastic_index: Optional[str] = None,
    splunk_index: Optional[str] = None,
    wazuh_index: Optional[str] = None,
) -> Tuple[List[Source], List[str]]:
    """The SIEMs Vigil reads, as sources; and why any is not read."""
    from core.detections.inventory import kibana, rollout, sees, splunk, wazuh_sees

    sources: List[Source] = []
    skipped: List[str] = []
    config = rollout._elastic_config()
    if config is None:
        skipped.append(
            "Elastic: elastic-siem is not enabled or has no elasticsearch_url"
        )
    else:
        per_hour = config.get("max_searches_per_hour")
        per_hour = sees.DEFAULT_PER_HOUR if per_hour is None else max(int(per_hour), 0)
        sources.append(
            ElasticSource(
                kibana.service_of(config),
                elastic_index or ELASTIC_INDICES,
                per_hour,
                rollout.INTEGRATION,
            )
        )
        why, _ = wazuh_sees.indexer()
        if why is None:
            sources.append(
                WazuhSource(
                    kibana.service_of(config),
                    WAZUH_ARCHIVES,
                    per_hour,
                    rollout.INTEGRATION,
                )
            )
    splunk_config = rollout._splunk_config()
    if splunk_config is None:
        skipped.append("Splunk: not enabled or missing its login")
    else:
        sources.append(
            SplunkSource(
                splunk.service_of(splunk_config),
                splunk_index or SPLUNK_INDEX,
                splunk.per_hour_of(splunk_config),
            )
        )
    return sources, skipped


# --- the replay -------------------------------------------------------------------


def _plain_list(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


async def _replay_one(
    source: Source,
    matcher: Any,
    fields: Sequence[str],
    attack_claim: Optional[Claim],
    named: Sequence[str],
    claims: Sequence[Claim],
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {"siem": source.siem, "index": source.index}
    if not source.spend("attack") or not source.spend("benign"):
        out["deferred"] = "its search budget for this hour is spent"
        return out
    try:
        attack_read = await source.attack(attack_claim, named, fields, start, end)
        benign_read = await source.benign(fields, start, end)
    except Exception as exc:  # a SIEM's own error, named on the step
        logger.warning("proposal replay: %s read failed: %s", source.siem, exc)
        out["error"] = str(exc) or type(exc).__name__
        return out
    attack = [event_of(source.siem, d) for d in attack_read.docs]
    if attack_claim is not None and not named:
        # Entity terms are wide; keep only what the claim really holds.
        attack = [e for e in attack if attack_claim.holds(e)]
    attack_ids = {e.id for e in attack}
    # The Case's own events inside its padded window are never normal ones, read as
    # attack or not.
    claimed = [c for c in claims if not c.ruled_out]
    if attack_claim is not None and not named:
        claimed.append(attack_claim)
    benign: List[Event] = []
    left_out = 0
    for doc in benign_read.docs:
        event = event_of(source.siem, doc)
        if event.id in attack_ids:
            continue
        if any(c.holds(event) for c in claimed):
            left_out += 1
            continue
        benign.append(event)
    holding = [e for e in benign if any(f in e.view for f in fields)]
    hits = [e for e in attack if matcher(e.view)]
    false = [e for e in holding if matcher(e.view)]
    out.update(
        attack_rows=len(attack),
        attack_cut_short=attack_read.cut_short,
        hits=len(hits),
        hit_samples=[e.sample(fields) for e in hits[:SAMPLES]],
        benign_rows=len(benign),
        benign_holding_fields=len(holding),
        benign_cut_short=benign_read.cut_short,
        benign_total=benign_read.total,
        claimed_left_out=left_out,
        false_positives=len(false),
        false_positive_samples=[e.sample(fields) for e in false[:SAMPLES]],
    )
    return out


def _unproven_because(
    per_siem: Sequence[Dict[str, Any]], skipped: Sequence[str]
) -> Optional[str]:
    read = [s for s in per_siem if "hits" in s]
    reasons: List[str] = []
    for s in per_siem:
        if s.get("deferred"):
            reasons.append(f"{s['siem'].title()} was not read: {s['deferred']}.")
        if s.get("error"):
            reasons.append(f"{s['siem'].title()} could not be read: {s['error']}")
    if not read:
        reasons.append(
            "No SIEM was read." + (f" {' '.join(skipped)}." if skipped else "")
        )
        return " ".join(reasons)
    false = sum(s["false_positives"] for s in read)
    if false:
        where = [
            f"{s['false_positives']} in {s['siem'].title()}"
            for s in read
            if s["false_positives"]
        ]
        reasons.append(
            f"It matched {false} event{'s' if false != 1 else ''} no Case claims "
            f"({_plain_list(where)}). They may be normal activity, or an attack nobody "
            "linked to a Case: see the samples."
        )
    if not sum(s["hits"] for s in read):
        attack_rows = sum(s["attack_rows"] for s in read)
        if not attack_rows:
            reasons.append(
                "No attack event was read: name the events, or link the proposal to "
                "a Case whose hosts, users or addresses the SIEM holds."
            )
        else:
            # A cut-short attack read can only miss hits, so it matters here alone.
            cut_attack = [s["siem"].title() for s in read if s["attack_cut_short"]]
            reasons.append(
                "It matched none of the attack events"
                + (
                    f" read (only the newest of the Case's events were read in "
                    f"{_plain_list(cut_attack)})."
                    if cut_attack
                    else "."
                )
            )
    if not sum(s["benign_holding_fields"] for s in read):
        reasons.append(
            "No normal event in the window holds a field the rule reads, so nothing "
            "shows it stays quiet."
        )
    # A cut-short normal read may have missed a false positive: never proven.
    cut = [s["siem"].title() for s in read if s["benign_cut_short"]]
    if cut:
        reasons.append(
            f"The read of normal events was cut short in {_plain_list(cut)}: only "
            "the newest were checked."
        )
    return " ".join(reasons) or None


async def replay(
    sigma: str,
    *,
    sources: Sequence[Source],
    attack_claim: Optional[Claim],
    named: Optional[Mapping[str, Sequence[str]]] = None,
    claims: Sequence[Claim] = (),
    start: datetime,
    end: datetime,
    skipped: Sequence[str] = (),
) -> Dict[str, Any]:
    """The replay's evidence, as the step record stores it."""
    evidence: Dict[str, Any] = {
        "step": "replay",
        "tool": "core.detections.engineering.siem_replay.replay",
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "attack_basis": (
            "named by a person"
            if named
            else (f"linked to {attack_claim.ref}" if attack_claim else "none")
        ),
        "skipped": list(skipped),
    }
    fields = rule_fields(sigma)
    evidence["rule_fields"] = fields
    try:
        matcher = _compile_rule(sigma)
    except _Unevaluable as exc:
        evidence.update(
            evaluated=False,
            proven=False,
            unproven_because=f"The rule cannot be replayed: {exc}.",
        )
        return evidence
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        evidence.update(
            evaluated=False,
            proven=False,
            unproven_because=f"The rule cannot be read: {exc}.",
        )
        return evidence
    per_siem = [
        await _replay_one(
            source,
            matcher,
            fields,
            attack_claim,
            list((named or {}).get(source.siem, ())),
            claims,
            start,
            end,
        )
        for source in sources
    ]
    read = [s for s in per_siem if "hits" in s]
    because = _unproven_because(per_siem, skipped)
    evidence.update(
        # Evaluated only when a SIEM was read: a replay that read nothing judged nothing.
        evaluated=bool(read),
        siems=per_siem,
        hits=sum(s["hits"] for s in read),
        attack_rows=sum(s["attack_rows"] for s in read),
        false_positives=sum(s["false_positives"] for s in read),
        benign_rows=sum(s["benign_rows"] for s in read),
        benign_holding_fields=sum(s["benign_holding_fields"] for s in read),
        proven=because is None,
        unproven_because=because,
    )
    return evidence


async def run(
    sigma: str,
    *,
    case_id: Optional[str] = None,
    named: Optional[Mapping[str, Sequence[str]]] = None,
    days: int = WINDOW_DAYS,
    elastic_index: Optional[str] = None,
    splunk_index: Optional[str] = None,
    wazuh_index: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Replay ``sigma`` on the SIEMs Vigil reads, over the last ``days``."""
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    claims = await asyncio.to_thread(ledger_claims, days)
    attack_claim = None
    if case_id and not named:
        attack_claim = await asyncio.to_thread(case_claim, case_id, claims)
    sources, skipped = configured_sources(elastic_index, splunk_index, wazuh_index)
    try:
        return await replay(
            sigma,
            sources=sources,
            attack_claim=attack_claim,
            named=named,
            claims=claims,
            start=start,
            end=end,
            skipped=skipped,
        )
    finally:
        for source in sources:
            try:
                await source.close()
            except Exception as exc:  # closing must not hide the replay
                logger.warning("proposal replay: closing %s: %s", source.siem, exc)
