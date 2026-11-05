"""Rollout: Vigil runs its overlay copies and moves them shadow -> live -> back.

Each daemon sync tick, before ``sync_repos`` (so Sees spends only what is left),
every active overlay copy whose current stage (its latest stage row) is shadow
or live is run over a window that runs ``LAG`` behind now: from where its last
successful run stopped (or an hour before that) up to now - LAG, so an event
stamped earlier but indexed late still lands in a window not yet searched, and
is counted once. An Elastic copy runs against the elastic-siem Elasticsearch:
EQL through ``_eql/search``, ES|QL through ``_query``, Lucene through
``_search``. A Splunk copy runs its SPL (the ESCU YAML's ``search``) against the
splunk integration; its hit count is the result rows (an index Vigil's Splunk
user cannot search is Splunk Sees' unknown). A copy whose checks['sees'] reads
unknown because Vigil's user cannot read its index (Splunk's "cannot search
index=…", Elastic's "lacks read on …") is held: not searched, nothing spent,
Runs unknown with Sees' reason, its stage and last successful run left as they
are, so it resumes over the gap once Sees reads again. Each run
spends one search of its SIEM's budget (key ``overlay:<rule id>``), least
recently run first; once the hour's budget is spent the rest wait. A copy an
active SIEM copy linked to it now runs (``handed_off``) is neither searched nor
spent for, its stage is left as is, and its Runs is dropped (a NULL row closes
its flag).

A run writes checks['runs'] on the latest version (result, reason, last
successful run, consecutive failures, per-day pass/fail), recording a changed
result through ``_record_changes`` so ``rule_health`` flags a broken copy. Its
hits add to checks['shadow'] or checks['live'] per-day counts; a shadow copy
also keeps a small sample, a live copy's hits become findings for the caller to
queue. A run fetches at most ``MAX_DOCS`` documents (its count is the full
total): a live copy past that in one run is over the daily hit limit and
reverts in the same tick.

A copy's active exceptions (``core/detections/exceptions.py``) drop the fetched
documents they match before any of that: a dropped document is not in the
day's hit count and becomes no finding, and checks['exceptions'] counts each
exception's matches per day. A handed-off copy is not run, so its exceptions
are not applied; alerts the SIEM itself raises are never filtered.

The sweep (after ``sync_repos``) then writes a stage row (actor 'system', proof
holding the numbers) for each move that is due: shadow -> live after a quiet,
passing week with checks['sees'] pass (at most ``LIVE_PER_DAY`` a day across
all rules), live -> shadow when half its findings are judged not real
(``accurate.verdicts``: the latest analyst determination wins), a loud day or a
Sees fail (its data stopped), shadow -> off after ``OFF_AFTER_FAILURES``
failed runs in a row. No stage row is written while Stop is set, or while the
ceiling cannot be read. A copy's move to live, when a git source is marked
``deploy_target`` for its format (elastic or splunk), queues one
``agent-run-mitre_analyst`` run that opens a pull request adding the rule there:
the overlay TOML or ESCU YAML file exactly as stored.

While Stop is set (or the ceiling cannot be read) no copy is run: no search,
no checks written, no findings. Its last successful run stays put, so the stopped
period is searched on the first run after Stop lifts.

Nothing is written to Elasticsearch, Kibana or Splunk.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Set, Tuple

from sqlalchemy import (
    and_,
    bindparam,
    case,
    exists,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.orm import aliased

from core.detections import exceptions
from core.detections.inventory import accurate, kibana, readers, sees, splunk
from core.detections.inventory.catalog import REPO_PREFIX
from core.integrations._base import search_budget
from core.integrations._base.ids import EXTERNAL_ID_MAX, fit_id
from core.integrations.elastic.descriptor import ELASTIC
from core.integrations.splunk.descriptor import SPLUNK, SPLUNK_SELFHOSTED_REQUIRED
from core.storage.connection import get_db_manager
from core.storage.models import (
    DetectionRule,
    DetectionRuleStage,
    DetectionRuleVersion,
    Finding,
    IntakeTrigger,
)

logger = logging.getLogger(__name__)

INTEGRATION = ELASTIC.id
LANGUAGES = ("eql", "esql", "lucene")
FIRST_WINDOW = timedelta(hours=1)
LAG = timedelta(minutes=30)  # how late an event may be indexed and still be seen
MAX_DOCS = 100  # documents fetched per run; the count is the full total
SAMPLE = 3
SAMPLE_CHARS = 500
KEEP_DAYS = 14
SHADOW_DAYS = 7
MAX_HITS_PER_DAY = 20
LIVE_PER_DAY = 5
REVERT_MIN_FINDINGS = 10
REVERT_NOISE_SHARE = 0.5
OFF_AFTER_FAILURES = 3
# Links under which a SIEM copy runs an overlay rule in its place.
HANDOFF_LINKS = ("same_id", "known_name", "agent")


def _budget_key(rule_id: int) -> str:
    # The prefix keeps it apart from Sees' "<indices>|<field>" keys.
    return f"overlay:{rule_id}"


@dataclass
class Copy:
    rule_id: int
    version: int
    name: str
    language: str  # "elastic" or "splunk"
    stage: str
    stage_at: datetime
    checks: Dict[str, Any]
    rule: Dict[str, Any]
    key: str = ""  # its rule_id, the overlay rule's native_key
    toml: str = ""  # the overlay file exactly as stored
    path: Optional[str] = None


@dataclass
class RolloutReport:
    source: str = "overlay:rollout"
    copies: int = 0
    searched: int = 0
    deferred: int = 0
    handed_off: int = 0  # copies a linked SIEM copy runs instead
    written: int = 0
    moves: List[str] = field(default_factory=list)
    held: int = 0  # due moves not written: Stop set or the ceiling unread
    queued: List[int] = field(default_factory=list)  # intake rows for deploy PRs
    findings: List[Dict[str, Any]] = field(default_factory=list, repr=False)
    error: Optional[str] = None
    skipped: List[str] = field(default_factory=list)


def _toml_rule(content: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        rule = tomllib.loads(str(content.get("_content") or "")).get("rule")
    except tomllib.TOMLDecodeError:
        return {}
    return rule if isinstance(rule, dict) else {}


def _rule_of(language: str, content: Mapping[str, Any]) -> Dict[str, Any]:
    # A Splunk copy's content is its ESCU YAML as JSON; an Elastic one's is its TOML.
    return dict(content) if language == "splunk" else _toml_rule(content)


def _copies() -> List[Copy]:
    """Active overlay copies at their latest version, with their current stage."""
    s = DetectionRuleStage
    current = (
        select(s.rule_id, s.stage, s.at)
        .distinct(s.rule_id)
        .order_by(s.rule_id, s.at.desc(), s.id.desc())
        .subquery()
    )
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRule.id,
                DetectionRule.latest_version,
                DetectionRule.name,
                DetectionRule.language,
                current.c.stage,
                current.c.at,
                DetectionRuleVersion.checks,
                DetectionRuleVersion.content,
                DetectionRule.native_key,
                DetectionRule.path,
            )
            .join(
                DetectionRuleVersion,
                and_(
                    DetectionRuleVersion.rule_id == DetectionRule.id,
                    DetectionRuleVersion.version == DetectionRule.latest_version,
                ),
            )
            .join(current, current.c.rule_id == DetectionRule.id)
            .where(
                DetectionRule.source == readers.OVERLAY_SOURCE,
                DetectionRule.language.in_(("elastic", "splunk")),
                DetectionRule.status == "active",
            )
            .order_by(DetectionRule.id)
        ).all()
    return [
        Copy(
            rid,
            ver,
            name,
            lang,
            stage,
            at,
            checks or {},
            _rule_of(lang, content or {}),
            key,
            str((content or {}).get("_content") or ""),
            path,
        )
        for rid, ver, name, lang, stage, at, checks, content, key, path in rows
    ]


def _elastic_config() -> Optional[Dict[str, Any]]:
    """elastic-siem's resolved config, read as ``sync._sync_elastic`` reads it."""
    from core.config import load_integrations_config
    from core.integrations._base.config import missing, resolve

    integrations = load_integrations_config()
    if not integrations["configured"]:
        return None
    if INTEGRATION not in integrations["enabled_integrations"]:
        return None
    config = resolve(ELASTIC)
    return None if missing(config, "elasticsearch_url") else config


def _splunk_config() -> Optional[Dict[str, Any]]:
    """splunk's resolved config, read as ``sync._sync_splunk`` reads it."""
    from core.config import load_integrations_config
    from core.integrations._base.config import missing, resolve

    integrations = load_integrations_config()
    if not integrations["configured"]:
        return None
    if SPLUNK.id not in integrations["enabled_integrations"]:
        return None
    config = resolve(SPLUNK)
    return None if missing(config, *SPLUNK_SELFHOSTED_REQUIRED) else config


def handed_off(rule_ids: List[int]) -> Set[int]:
    """Of these overlay rule ids, those an active SIEM copy runs instead.

    That is a Splunk saved search (for a Splunk copy) or a Kibana rule (for an
    Elastic one) linked by the sync (same_id / known_name) or an agent to the
    overlay rule, or to an active repo rule of its language carrying its key
    (rule_id). A Splunk copy also matches a repo rule by name, since ESCU links
    by name (known_name); an Elastic copy matches by rule_id only.
    """
    if not rule_ids:
        return set()
    o, r, s = aliased(DetectionRule), aliased(DetectionRule), aliased(DetectionRule)
    same_rule = (
        select(r.id)
        .where(
            r.source.startswith(REPO_PREFIX),
            r.status == "active",
            r.language == o.language,
            or_(
                r.native_key == o.native_key,
                and_(o.language == "splunk", r.name == o.name),
            ),
        )
        .correlate(o)
    )
    linked = exists().where(
        s.source == case((o.language == "splunk", splunk.SOURCE), else_=kibana.SOURCE),
        s.status == "active",
        s.link_kind.in_(HANDOFF_LINKS),
        or_(s.origin_rule_id == o.id, s.origin_rule_id.in_(same_rule)),
    )
    with get_db_manager().session_scope() as session:
        ids = session.scalars(
            select(o.id).where(o.id.in_(list(rule_ids)), linked)
        ).all()
    return set(ids)


def runnable(rule: Mapping[str, Any]) -> Optional[str]:
    """Why the runner cannot run this Elastic [rule] table, or None when it can."""
    language = rule.get("language")
    if language not in LANGUAGES:
        return (
            f"language {language!r} is not run: write EQL (eql), ES|QL (esql) "
            "or Lucene (lucene)"
        )
    if language != "esql" and not _index(rule):
        return f'an {language} rule needs an index: add index = ["<pattern>"] to [rule]'
    return None


def _index(rule: Mapping[str, Any]) -> str:
    index = rule.get("index") or []
    if isinstance(index, str):
        index = [index]
    return ",".join(p for p in index if isinstance(p, str) and p)


async def _query(
    service: Any, copy: Copy, start: datetime, end: datetime
) -> Tuple[int, List[Dict[str, Any]]]:
    rule = copy.rule
    ts = rule.get("timestamp_override") or "@timestamp"
    window = {ts: {"gte": start.isoformat(), "lt": end.isoformat()}}
    query = str(rule.get("query") or "")
    language = rule["language"]
    if language == "eql":
        return await service.eql_search(_index(rule), query, window, MAX_DOCS)
    if language == "esql":
        return await service.esql_query(query, window)
    return await service.lucene_search(_index(rule), query, window, MAX_DOCS)


async def _splunk_query(
    service: Any, copy: Copy, start: datetime, end: datetime
) -> Tuple[int, List[Dict[str, Any]]]:
    # Epoch ends, not "now": the next run starts where this one ended.
    found = await asyncio.to_thread(
        service.search,
        str(copy.rule.get("search") or ""),
        earliest_time=f"{start.timestamp():.3f}",
        latest_time=f"{end.timestamp():.3f}",
        max_count=MAX_DOCS,
        # Every field, so an exception on one the SPL doesn't name can match.
        all_fields=True,
    )
    # Rows, not event_count: that is events scanned, wrong for transforming SPL.
    rows = found.results[:MAX_DOCS]
    return len(rows), [_splunk_doc(row) for row in rows]


def _digest(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


def _splunk_doc(row: Mapping[str, Any]) -> Dict[str, Any]:
    """A result row in the shape of an Elasticsearch hit."""
    if row.get("_cd"):
        # _cd is per bucket; _bkt (index~id~guid) makes it unique across indexes.
        hit_id = ":".join(str(row[k]) for k in ("_bkt", "_cd") if row.get(k))
    elif row.get("_raw") is not None:
        hit_id = _digest([row.get("_raw"), row.get("_time")])
    else:
        hit_id = _digest(row)
    return {"_id": hit_id, "_index": row.get("index"), "_source": dict(row)}


def _doc_id(doc: Mapping[str, Any]) -> str:
    if doc.get("_id"):
        return str(doc["_id"])
    # ES|QL rows carry no _id unless the query asks for METADATA _id.
    return _digest(doc.get("_source"))


def _sample(doc: Mapping[str, Any]) -> Dict[str, Any]:
    source = json.dumps(doc.get("_source"), default=str)
    return {
        "_id": _doc_id(doc),
        "_index": doc.get("_index"),
        "_source": source[:SAMPLE_CHARS],
    }


def _trimmed(days: Dict[str, Any], today: str) -> Dict[str, Any]:
    cutoff = (
        (datetime.fromisoformat(today) - timedelta(days=KEEP_DAYS - 1))
        .date()
        .isoformat()
    )
    return {d: v for d, v in sorted(days.items()) if d >= cutoff}


def _window(copy: Copy, now: datetime) -> Tuple[datetime, datetime]:
    """[start, end) a run searches: from the last window's end to now - LAG."""
    end = now - LAG
    since = (copy.checks.get("runs") or {}).get("last_success")
    start = datetime.fromisoformat(since) if since else end - FIRST_WINDOW
    return start, max(start, end)  # never moves back over a searched window


def _ran(
    checks: Dict[str, Any],
    stage: str,
    end: datetime,
    outcome: Tuple[Optional[int], List[Dict], Optional[str]],
    searched_to: datetime,
    excepted: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """``checks`` after one run: Runs, and on a pass the stage's per-day hits
    and each exception's per-day matches."""
    total, docs, error = outcome
    day = end.date().isoformat()
    runs = dict(checks.get("runs") or {})
    days = dict(runs.get("days") or {})
    tally = dict(days.get(day) or {"pass": 0, "fail": 0})
    if error:
        runs.update(
            result="fail", reason=error, failures=int(runs.get("failures") or 0) + 1
        )
        tally["fail"] += 1
    else:
        runs.update(result="pass", reason=None, failures=0)
        runs["last_success"] = searched_to.isoformat()  # the next window's start
        tally["pass"] += 1
    days[day] = tally
    runs.update(days=_trimmed(days, day), checked_at=end.isoformat())
    out = {**checks, "runs": runs}
    if not error:
        block = dict(checks.get(stage) or {})
        hits = dict(block.get("days") or {})
        hits[day] = int(hits.get(day) or 0) + int(total or 0)
        block["days"] = _trimmed(hits, day)
        if stage == "shadow" and docs:
            block["sample"] = [_sample(d) for d in docs[:SAMPLE]]
        out[stage] = block
        if excepted:
            counts = dict(checks.get("exceptions") or {})
            for eid, n in excepted.items():
                per_day = dict((counts.get(eid) or {}).get("days") or {})
                per_day[day] = int(per_day.get(day) or 0) + n
                counts[eid] = {"days": _trimmed(per_day, day)}
            out["exceptions"] = counts
    return out


# The few fields triage keys on: ECS paths, else Splunk CIM names.
_ENTITY_FIELDS = (
    ("hostnames", ("host", "name"), ("dest", "host")),
    ("usernames", ("user", "name"), ("user",)),
    ("src_ips", ("source", "ip"), ("src_ip", "src")),
    ("dest_ips", ("destination", "ip"), ("dest_ip",)),
)


def _entities(source: Mapping[str, Any], language: str) -> Dict[str, List[str]]:
    """The few fields triage keys on, when the hit has them."""

    def get(*path: str) -> Any:
        value: Any = source
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        return value if value is not None else source.get(".".join(path))

    out: Dict[str, List[str]] = {}
    for key, path, cim in _ENTITY_FIELDS:
        if language == "splunk":
            value = next((source[f] for f in cim if source.get(f)), None)
        else:
            value = get(*path)
        if isinstance(value, (str, int)):
            out[key] = [str(value)]
    return out


def _finding(copy: Copy, doc: Mapping[str, Any], end: datetime) -> Dict[str, Any]:
    hit_id = _doc_id(doc)
    digest = hashlib.sha256(f"{copy.rule_id}:{hit_id}".encode()).hexdigest()[:32]
    source = doc.get("_source") if isinstance(doc.get("_source"), dict) else {}
    if copy.language == "splunk":
        ts = "_time"
    else:
        ts = copy.rule.get("timestamp_override") or "@timestamp"
    stamp = source.get(ts) if isinstance(source.get(ts), str) else end.isoformat()
    severity = str(copy.rule.get("severity") or "medium").lower()
    return {
        "finding_id": f"overlay-{digest}",
        "external_id": fit_id("", f"overlay:{copy.rule_id}:{hit_id}", EXTERNAL_ID_MAX),
        "data_source": copy.language,
        "timestamp": stamp,
        "severity": severity,
        "title": str(copy.rule.get("name") or copy.name),
        "description": str(copy.rule.get("description") or "")[:500],
        "entity_context": {
            **_entities(source, copy.language),
            "detection_rule": {"rule_id": copy.rule_id, "version": copy.version},
        },
        "raw_data": dict(doc),  # becomes source_evidence.records
    }


Query = Callable[[Any, Copy, datetime, datetime], Awaitable[Tuple[int, List[Dict]]]]


async def _run_all(
    integration: str,
    service: Any,
    query: Query,
    copies: List[Copy],
    per_hour: int,
    now: datetime,
    report: RolloutReport,
) -> Dict[int, Tuple[Optional[int], List[Dict], Optional[str]]]:
    """(total, docs, error) by rule id, for the copies ``integration``'s budget reached."""
    results: Dict[int, Tuple[Optional[int], List[Dict], Optional[str]]] = {}
    last = search_budget.last_spent(
        integration, [_budget_key(c.rule_id) for c in copies]
    )
    never = datetime.min.replace(tzinfo=timezone.utc)
    order = sorted(copies, key=lambda c: last.get(_budget_key(c.rule_id), never))
    for n, copy in enumerate(order):
        if not search_budget.spend(integration, per_hour, _budget_key(copy.rule_id)):
            report.deferred += len(order) - n
            break
        report.searched += 1
        try:
            total, docs = await query(service, copy, *_window(copy, now))
            results[copy.rule_id] = (total, docs, None)
        except Exception as exc:  # noqa: BLE001 -- the run fails with its message
            results[copy.rule_id] = (None, [], str(exc)[:500] or type(exc).__name__)
    return results


async def _run_elastic(
    config: Mapping[str, Any],
    copies: List[Copy],
    per_hour: int,
    now: datetime,
    report: RolloutReport,
) -> Dict[int, Tuple[Optional[int], List[Dict], Optional[str]]]:
    service = kibana.service_of(config)
    try:
        return await _run_all(
            INTEGRATION, service, _query, copies, per_hour, now, report
        )
    finally:
        await service.close()


def _write_runs(
    copies: List[Copy],
    results: Dict[int, tuple],
    now: datetime,
    excepted: Optional[Dict[int, Dict[str, int]]] = None,
) -> int:
    """Merge each run into its version's checks; record changed Runs results."""
    from core.detections.inventory.sync import _change, _last_results, _record_changes

    if not results:
        return 0
    stamp = now.isoformat()
    by_id = {c.rule_id: c for c in copies}
    with get_db_manager().session_scope() as session:
        last = _last_results(session, readers.OVERLAY_SOURCE)
        rows = session.execute(
            select(
                DetectionRuleVersion.rule_id,
                DetectionRuleVersion.version,
                DetectionRuleVersion.checks,
            ).where(
                DetectionRuleVersion.rule_id.in_(list(results)),
                DetectionRuleVersion.version.in_(list({c.version for c in copies})),
            )
        ).all()
        changed, changes = [], []
        for rid, ver, checks in rows:
            copy = by_id[rid]
            if ver != copy.version:
                continue
            new = _ran(
                checks or {},
                copy.stage,
                now,
                results[rid],
                _window(copy, now)[1],
                (excepted or {}).get(rid),
            )
            runs = new["runs"]
            before = last.get((rid, "runs"))
            if before != (runs["result"], runs["reason"]):
                changes.append(
                    _change(
                        rid,
                        ver,
                        "runs",
                        before[0] if before else None,
                        runs["result"],
                        runs["reason"],
                    )
                )
            changed.append({"rid": rid, "ver": ver, "new_checks": new})
        if changed:
            session.connection().execute(
                update(DetectionRuleVersion)
                .where(
                    DetectionRuleVersion.rule_id == bindparam("rid"),
                    DetectionRuleVersion.version == bindparam("ver"),
                )
                .values(checks=bindparam("new_checks")),
                changed,
            )
        _record_changes(session, changes, stamp)
    return len(changed)


def _hand_off(copies: List[Copy], now: datetime) -> None:
    """Copies their SIEM runs now stop carrying Runs: a NULL row closes their flag."""
    from core.detections.inventory.sync import (
        _dropped,
        _last_results,
        _record_changes,
        _write_checks,
    )

    if not copies:
        return
    with get_db_manager().session_scope() as session:
        last = _last_results(session, readers.OVERLAY_SOURCE)
        changes, checked = [], []
        for c in copies:
            rows, checks = _dropped(
                c.rule_id,
                c.version,
                c.checks,
                ("runs",),
                last,
                "run by Splunk" if c.language == "splunk" else "run by Kibana",
            )
            changes += rows
            if checks != c.checks:
                checked.append((c.rule_id, c.version, checks))
        _write_checks(session, checked)
        _record_changes(session, changes, now.isoformat())


def _held(copies: List[Copy]) -> List[Tuple[Copy, str]]:
    """Copies Sees says sit on an index Vigil's user can't read, with its reason."""
    return [
        (c, str(sees_.get("reason") or "Vigil's user cannot read its index"))
        for c in copies
        if (sees_ := c.checks.get("sees") or {}).get("unreadable")
        and sees_.get("result") == "unknown"
    ]


def _write_unrunnable(copies: List[Tuple[Copy, str]], now: datetime) -> int:
    """checks['runs'] unknown for copies the runner cannot run, once per reason.

    Its other fields stay: last_success is where a held copy resumes."""
    results = {
        (c.rule_id, c.version): {
            **(c.checks.get("runs") or {}),
            "result": "unknown",
            "reason": why,
        }
        for c, why in copies
        if (c.checks.get("runs") or {}).get("reason") != why
    }
    return (
        sees.write_check("runs", results, source=readers.OVERLAY_SOURCE)
        if results
        else 0
    )


def _noise(session: Any, copy: Copy) -> Tuple[int, int]:
    """(findings naming the version since it went live, how many were judged not real)."""
    since = copy.stage_at.astimezone(timezone.utc).replace(tzinfo=None)
    found = accurate.verdicts(
        session,
        Finding.entity_context["detection_rule"].contains(
            {"rule_id": copy.rule_id, "version": copy.version}
        ),
        Finding.created_at >= since,
    )
    noise = sum(1 for v in found.values() if v is not None and not v[0])
    return len(found), noise


def _week(now: datetime) -> List[str]:
    return [(now - timedelta(days=i)).date().isoformat() for i in range(SHADOW_DAYS)]


def _due(
    session: Any, copy: Copy, now: datetime, live_24h: int
) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """(stage, reason, proof) of the move due for ``copy``, or None."""
    runs = copy.checks.get("runs") or {}
    sees_check = copy.checks.get("sees") or {}
    seen = {
        k: sees_check.get(k) for k in ("result", "reason", "data_source", "checked_at")
    }
    since = copy.stage_at.isoformat()
    if copy.stage == "shadow":
        failures = int(runs.get("failures") or 0)
        if failures >= OFF_AFTER_FAILURES:
            return (
                "off",
                f"{failures} failed runs in a row",
                {"failures": failures, "last_reason": runs.get("reason")},
            )
        if now - copy.stage_at < timedelta(days=SHADOW_DAYS):
            return None
        week = _week(now)
        run_days = runs.get("days") or {}
        hit_days = (copy.checks.get("shadow") or {}).get("days") or {}
        days = {
            d: {
                "hits": int(hit_days.get(d) or 0),
                "pass": int((run_days.get(d) or {}).get("pass") or 0),
                "fail": int((run_days.get(d) or {}).get("fail") or 0),
            }
            for d in week
        }
        if any(v["pass"] == 0 or v["fail"] for v in days.values()):
            return None
        if any(v["hits"] > MAX_HITS_PER_DAY for v in days.values()):
            return None
        if seen.get("result") != "pass":  # its data must be shown to arrive
            return None
        if live_24h >= LIVE_PER_DAY:
            return None
        return (
            "live",
            f"{SHADOW_DAYS} days in shadow, every day's runs passed, no day "
            f"above {MAX_HITS_PER_DAY} hits, Sees passed",
            {
                "shadow_since": since,
                "days": days,
                "max_hits_per_day": max(v["hits"] for v in days.values()),
                "live_rows_last_24h": live_24h,
                "sees": seen,
            },
        )
    # live: only what happened since it went live counts.
    start = copy.stage_at.date().isoformat()
    hit_days = {
        d: n
        for d, n in ((copy.checks.get("live") or {}).get("days") or {}).items()
        if d >= start
    }
    loud = {d: n for d, n in hit_days.items() if n > MAX_HITS_PER_DAY}
    if loud:
        return (
            "shadow",
            f"a live day above {MAX_HITS_PER_DAY} hits",
            {"live_since": since, "loud_days": loud},
        )
    if seen["result"] == "fail":  # lost data; unknown changes nothing
        return (
            "shadow",
            f"Sees: {seen['reason'] or 'fail'}",
            {"live_since": since, "sees": seen},
        )
    total, noise = _noise(session, copy)
    if total >= REVERT_MIN_FINDINGS and noise >= total * REVERT_NOISE_SHARE:
        return (
            "shadow",
            f"{noise} of {total} findings judged not real",
            {"live_since": since, "findings": total, "noise": noise},
        )
    return None


def _stopped() -> bool:
    from core.response.ceiling import try_read_stored_ceiling

    held = try_read_stored_ceiling()
    return held is None or held[0].stopped


def sweep(now: datetime, report: RolloutReport) -> None:
    """Write a stage row for each move due, unless Stop is set."""
    from core.detections.inventory.sync import _stage_row

    copies = [c for c in _copies() if c.stage in ("shadow", "live")]
    # A handed-off copy keeps its stage: Vigil no longer runs it.
    skip = handed_off([c.rule_id for c in copies])
    copies = [c for c in copies if c.rule_id not in skip]
    stopped = _stopped()
    targets = _deploy_targets()
    if targets.get("splunk"):
        # Off disk before the transaction: a read must not roll back stage rows.
        _read_splunk_files([c for c in copies if c.stage == "shadow"])
    with get_db_manager().session_scope() as session:
        live_24h = session.scalar(
            select(func.count())
            .select_from(DetectionRuleStage)
            .where(
                DetectionRuleStage.stage == "live",
                DetectionRuleStage.at >= now - timedelta(hours=24),
            )
        )
        rows, promoted = [], []
        for copy in copies:
            due = _due(session, copy, now, live_24h)
            if due is None:
                continue
            stage, reason, proof = due
            if stopped:
                report.held += 1
                continue
            rows.append(
                {
                    **_stage_row(copy.rule_id, copy.version, stage, reason),
                    "proof": proof,
                }
            )
            report.moves.append(
                f"{copy.rule_id} v{copy.version}: {copy.stage} -> {stage}"
            )
            live_24h += stage == "live"
            if stage == "live":
                promoted.append(copy)
        if rows:
            session.execute(insert(DetectionRuleStage), rows)
            # In the same transaction: a promotion and its PR run land together.
            for trigger in _deploy_runs(session, promoted, targets):
                session.add(trigger)
                session.flush()
                report.queued.append(trigger.id)


DEPLOY_WORKFLOW = "agent-run-mitre_analyst"


def _deploy_targets() -> Dict[str, Dict[str, Any]]:
    """Each format's deploy target, read before the stage transaction opens."""
    from core.detections.detection_rules_service import (
        DEPLOY_FORMATS,
        read_deploy_target,
    )

    targets = {}
    for fmt in DEPLOY_FORMATS:
        try:
            target = read_deploy_target(fmt)
        except (
            Exception
        ) as exc:  # noqa: BLE001 -- an unread target must not hold a promotion
            logger.warning("detection rules: %s deploy target unread: %s", fmt, exc)
            continue
        if target:
            targets[fmt] = target
    return targets


def _read_splunk_files(copies: List[Copy]) -> None:
    """Fill each Splunk copy's ``toml`` with its overlay YAML file as stored."""
    from core.config import vigil_path

    root = vigil_path("detections", readers.OVERLAY_ID)
    for copy in copies:
        if copy.language != "splunk":
            continue
        try:
            copy.toml = (root / copy.path).read_text() if copy.path else ""
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "detection rules: overlay file of rule %s unread: %s", copy.rule_id, exc
            )
        if not copy.toml:
            logger.warning(
                "detection rules: rule %s has no overlay file; no deploy PR",
                copy.rule_id,
            )


def _deploy_brief(copy: Copy, target: Dict[str, Any]) -> str:
    where = target.get("subdirectory") or "the repository root"
    splunk = copy.language == "splunk"
    return (
        f"Open a pull request adding the live Vigil rule {copy.name!r} "
        f"({'ESCU id' if splunk else 'rule_id'} {copy.key}, "
        f"Vigil detection rule {copy.rule_id}) to "
        f"{target['git_url']} under {where}, with the attached "
        f"{'ESCU YAML' if splunk else 'TOML'} "
        f"(Vigil's {copy.path or 'overlay file'}) as its file content "
        "exactly as given, and record the pull request's link on this Case."
    )


def _excepted_brief(session: Any, copy: Copy) -> str:
    """The copy's active exceptions for the PR description; the rule file has none."""
    rows = exceptions.listed(session, copy.rule_id)
    if not rows:
        return ""
    lines = "\n".join(
        f"- {json.dumps(r['match'], sort_keys=True)}: {r['note']}" for r in rows
    )
    return (
        " List these customer-specific exceptions in the pull request's "
        "description, not in the rule file: Vigil applied them to this rule's "
        "hits and stops once the SIEM runs it, so the reviewer can carry them into "
        "the SIEM's own exception mechanism (for example a Kibana exception "
        f"list).\n{lines}"
    )


def _deploy_runs(
    session: Any, promoted: List[Copy], targets: Mapping[str, Dict[str, Any]]
) -> List[IntakeTrigger]:
    """A queued mitre_analyst run per promoted copy whose format has a target."""
    return [
        IntakeTrigger(
            kind="schedule",
            state="queued",
            priority="low",
            payload={
                "workflow_id": DEPLOY_WORKFLOW,
                # Not "scheduled": the daily gaps run reads its clock off those.
                "trigger_type": "rollout",
                "finding_ids": [],
                "hypothesis": _deploy_brief(copy, targets[copy.language])
                + _excepted_brief(session, copy),
                # The overlay file whole, exclusions included; the brief carries it.
                "document": copy.toml,
            },
        )
        for copy in promoted
        # A Splunk copy whose file went unread has nothing to attach.
        if targets.get(copy.language) and (copy.language == "elastic" or copy.toml)
    ]


def _deploy_marker(rule_id: int) -> str:
    """The text every deploy brief for this rule carries (see _deploy_brief)."""
    return f"Vigil detection rule {rule_id})"


def overlay_only() -> List[Dict[str, Any]]:
    """Live overlay copies no SIEM rule runs: only Vigil detects with these, so
    Stop halts their detection. Each says whether its format has a deploy
    target and whether a deploy-PR run was already queued for it."""
    live = [c for c in _copies() if c.stage == "live"]
    gone = handed_off([c.rule_id for c in live])
    live = [c for c in live if c.rule_id not in gone]
    targets = _deploy_targets()
    with get_db_manager().session_scope() as session:
        queued = _deploys_queued(session, [c.rule_id for c in live])
    return [
        {
            "id": c.rule_id,
            "name": c.name,
            "language": c.language,
            "deploy_target": c.language in targets,
            "deploy_queued": c.rule_id in queued,
        }
        for c in live
    ]


def _deploys_queued(session: Any, rule_ids: List[int]) -> Set[int]:
    """Of these rule ids, those a deploy-PR run was ever queued for."""
    if not rule_ids:
        return set()
    briefs = session.scalars(
        select(IntakeTrigger.payload["hypothesis"].astext).where(
            IntakeTrigger.payload["workflow_id"].astext == DEPLOY_WORKFLOW
        )
    ).all()
    return {r for r in rule_ids if any(_deploy_marker(r) in (b or "") for b in briefs)}


def deploy_overlay_only(rule_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """Queue the deploy-PR run, as a promotion to live does, for live
    overlay-only copies (these ``rule_ids``, or all): only those whose format
    has a deploy target and with no deploy run queued yet. Says why each other
    one was skipped."""
    live = [c for c in _copies() if c.stage == "live"]
    gone = handed_off([c.rule_id for c in live])
    wanted = [
        c
        for c in live
        if c.rule_id not in gone and (rule_ids is None or c.rule_id in rule_ids)
    ]
    targets = _deploy_targets()
    if targets.get("splunk"):
        _read_splunk_files(wanted)
    skipped: Dict[int, str] = {}
    if rule_ids is not None:
        found = {c.rule_id for c in wanted}
        skipped.update(
            {r: "not a live rule only Vigil runs" for r in rule_ids if r not in found}
        )
    with get_db_manager().session_scope() as session:
        queued_before = _deploys_queued(session, [c.rule_id for c in wanted])
        todo = []
        for copy in wanted:
            if copy.rule_id in queued_before:
                skipped[copy.rule_id] = "a deploy pull request is already queued"
            elif not targets.get(copy.language):
                skipped[copy.rule_id] = f"no {copy.language} deploy target is set"
            elif copy.language == "splunk" and not copy.toml:
                skipped[copy.rule_id] = "its overlay file could not be read"
            else:
                todo.append(copy)
        queued = []
        for copy, trigger in zip(todo, _deploy_runs(session, todo, targets)):
            session.add(trigger)
            session.flush()
            queued.append({"rule_id": copy.rule_id, "trigger_id": trigger.id})
    return {
        "queued": queued,
        "skipped": [{"rule_id": r, "reason": why} for r, why in skipped.items()],
    }


def run_copies(live: bool) -> RolloutReport:
    """Run both SIEMs' overlay copies within their budgets.

    ``live`` False (no processor queue) skips live copies entirely, so their
    last successful run stays put and their hits are read next tick.
    """
    report = RolloutReport()
    now = datetime.now(timezone.utc)
    # Overlay searches and findings are Vigil's own activity: Stop halts them all.
    # Nothing is written, so last_success stays and the next run covers the gap.
    if _stopped():
        report.skipped.append("stopped")
        return report
    try:
        copies = [
            c for c in _copies() if c.stage == "shadow" or (c.stage == "live" and live)
        ]
        skip = handed_off([c.rule_id for c in copies])
        _hand_off([c for c in copies if c.rule_id in skip], now)
    except Exception as exc:  # noqa: BLE001 -- the sync still runs
        logger.warning("detection rules: reading overlay copies failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
        return report
    report.handed_off = len(skip)
    copies = [c for c in copies if c.rule_id not in skip]
    report.copies = len(copies)
    for language, step in (
        ("elastic", _run_elastic_copies),
        ("splunk", _run_splunk_copies),
    ):
        mine = [c for c in copies if c.language == language]
        if not mine:
            continue
        try:  # one SIEM unreachable leaves the other's runs standing
            step(mine, now, report)
        except Exception as exc:  # noqa: BLE001 -- the sync's own reports stand
            logger.warning("detection rules: %s overlay runs failed: %s", language, exc)
            report.error = report.error or f"{language}: {type(exc).__name__}: {exc}"
    return report


def run_sweep(report: RolloutReport) -> RolloutReport:
    """Write the stage rows due onto ``report``: its own step, so a Revert is
    still due when a SIEM is unreachable."""
    try:
        sweep(datetime.now(timezone.utc), report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("detection rules: overlay stage sweep failed: %s", exc)
        report.error = report.error or f"sweep: {type(exc).__name__}: {exc}"
    logger.info("detection rules: rollout %s", report)
    return report


def run(live: bool) -> RolloutReport:
    """Run the overlay copies within their budgets, then sweep their stages."""
    return run_sweep(run_copies(live))


def _except(
    results: Dict[int, tuple],
) -> Tuple[Dict[int, tuple], Dict[int, Dict[str, int]]]:
    """Results with the documents an active exception matches dropped, and the
    matches per exception id by rule id. The total drops by the same number."""
    ran = [rid for rid, (_, _, error) in results.items() if not error]
    excepted = exceptions.active(ran)
    out, counts = dict(results), {}
    for rid, rules in excepted.items():
        total, docs, error = results[rid]
        kept, counts[rid] = exceptions.split(docs, rules)
        out[rid] = (max(int(total or 0) - (len(docs) - len(kept)), 0), kept, error)
    return out, counts


def _finish(
    copies: List[Copy], results: Dict[int, tuple], now: datetime, report: RolloutReport
) -> None:
    results, excepted = _except(results)
    report.written += _write_runs(copies, results, now, excepted)
    for copy in copies:
        _, docs, error = results.get(copy.rule_id, (None, [], "not run"))
        if copy.stage == "live" and not error:
            report.findings += [_finding(copy, d, now) for d in docs]


def _run_elastic_copies(
    copies: List[Copy], now: datetime, report: RolloutReport
) -> None:
    config = _elastic_config()
    if config is None:
        report.skipped.append("elastic-siem not enabled or has no elasticsearch_url")
        return
    per_hour = config.get("max_searches_per_hour")
    per_hour = sees.DEFAULT_PER_HOUR if per_hour is None else max(int(per_hour), 0)
    blocked = [(c, why) for c in copies if (why := runnable(c.rule))]
    blocked += _held([c for c in copies if not runnable(c.rule)])
    held = {id(c) for c, _ in blocked}
    ready = [c for c in copies if id(c) not in held]
    results = asyncio.run(_run_elastic(config, ready, per_hour, now, report))
    report.written += _write_unrunnable(blocked, now)
    _finish(ready, results, now, report)


def _run_splunk_copies(
    copies: List[Copy], now: datetime, report: RolloutReport
) -> None:
    config = _splunk_config()
    if config is None:
        report.skipped.append("splunk not enabled or missing its login")
        return
    why = "a splunk copy needs a search: add search: <SPL> to its YAML"
    blocked = [(c, why) for c in copies if not str(c.rule.get("search") or "").strip()]
    blocked += _held([c for c in copies if str(c.rule.get("search") or "").strip()])
    held = {id(c) for c, _ in blocked}
    ready = [c for c in copies if id(c) not in held]
    results = asyncio.run(
        _run_all(
            splunk.SOURCE,
            splunk.service_of(config),
            _splunk_query,
            ready,
            splunk.per_hour_of(config),
            now,
            report,
        )
    )
    report.written += _write_unrunnable(blocked, now)
    _finish(ready, results, now, report)
