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
exception's matches per day, and after a run that fetched rows, its fields
none of them carried (``seen``: a transforming search's rows lack its input
fields, so an exception on one cannot match them, #3907) and how many it
dropped; checks['exceptions_seen'] keeps what they dropped together, so a copy
whose exceptions drop every row it fetches shows as silenced (#3994). A handed-off copy is not run, so its exceptions
are not applied; alerts the SIEM itself raises are never filtered.

The sweep (after ``sync_repos``) then writes a stage row (actor 'system', proof
holding the numbers) for each move that is due: shadow -> live after a quiet,
passing week with checks['sees'] pass and checks['lint'] pass (at most
the stored promotion cap a day across all rules (``rollout_limits``;
its shadow days and hit ceiling are stored there too and read on every sweep); a copy with no lint result yet waits
for the sync that lints it), live -> shadow when half its findings are judged
not real (``accurate.verdicts``: the latest analyst determination wins), a loud
day or a Sees fail (its data stopped), shadow -> off after
``OFF_AFTER_FAILURES`` failed runs in a row, shadow or live -> off when
checks['lint'] reads fail (sync never got to turn it off). No stage row is
written while Stop is set, or while the ceiling cannot be read. After those rows
commit, each live copy whose format (elastic or splunk) has a git source marked
``deploy_target`` and whose version passed lint and has no PR recorded gets one,
opened by fixed code over the GitHub REST API
(github.com, or GitHub Enterprise Server at ``<host>/api/v3``): branch
``vigil/rule-<id>-v<version>``, the overlay TOML or ESCU YAML file exactly as
stored under the target's subdirectory, its active exceptions listed in the
description. A branch or PR an earlier attempt left is reused. Once a version's
PR is open, Vigil closes each still-open PR it opened for an earlier version of
the same rule (its ``vigil/rule-<id>-v<n>`` branch, never a merged PR or one
Vigil didn't open), with a "Superseded by" comment, and records that on the old
version. The outcome (PR url, the file already on the default branch, or an
error) lands in checks['deploy']; a failed one is retried next sweep. None is
opened while Stop is set.

A rule that leaves live (a person's Revert, Vigil's own move back to shadow, or
Vigil turning it off) takes its deploy PR with it: the sweep closes each PR
recorded open on any of its versions, with a comment naming where the rule went,
who moved it and why (its current stage row), and records that on the version as
``withdrawn``. A merged PR, or one already closed (a person's answer), is left
as it is and recorded so. When that version goes live again its PR is reopened,
not replaced: same branch, same file, and the review thread with Vigil's close
comment stays in one place. A failed close is retried next sweep; none is closed
while Stop is set.

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
import re
import time
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
    or_,
    select,
    update,
)
from sqlalchemy.orm import aliased

from core.detections import exceptions
from core.detections.inventory import (
    accurate,
    kibana,
    readers,
    sees,
    splunk,
    wazuh,
    wazuh_sees,
    wazuh_shadow,
)
from core.detections.inventory.catalog import REPO_PREFIX
from core.detections.inventory.rollout_limits import DEFAULTS as DEFAULT_LIMITS
from core.detections.inventory.rollout_limits import (
    RolloutLimits,
    read_limits,
)
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
)

logger = logging.getLogger(__name__)

INTEGRATION = ELASTIC.id
LANGUAGES = ("eql", "esql", "lucene")
# Staged copies Vigil runs live; the Wazuh manager runs a live Wazuh copy.
VIGIL_RUNS = ("splunk", "elastic")
FIRST_WINDOW = timedelta(hours=1)
LAG = timedelta(minutes=30)  # how late an event may be indexed and still be seen
MAX_DOCS = 100  # documents fetched per run; the count is the full total
SAMPLE = 3
SAMPLE_CHARS = 500
KEEP_DAYS = 14
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
    held: bool = False  # a person reverted it: shadow until a person releases it


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
    asked: List[str] = field(default_factory=list)  # promotions put to a person
    deploys: List[str] = field(default_factory=list)  # "<rule> v<n>: <PR url | error>"
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
    # A Splunk copy's content is its ESCU YAML as JSON; an Elastic one's is its TOML;
    # a Wazuh one's XML is read by wazuh_shadow from Copy.toml.
    if language == "wazuh":
        return {}
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
    # A person's hold: the latest row with a 'hold' key, whatever rows follow it.
    hold = (
        select(s.rule_id, s.proof["hold"].astext.label("hold"))
        .where(s.proof.has_key("hold"))
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
                hold.c.hold,
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
            .outerjoin(hold, hold.c.rule_id == DetectionRule.id)
            .where(
                DetectionRule.source == readers.OVERLAY_SOURCE,
                DetectionRule.language.in_(("elastic", "splunk", "wazuh")),
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
            hold == "true",
        )
        for rid, ver, name, lang, stage, at, hold, checks, content, key, path in rows
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
        s.source
        == case(
            (o.language == "splunk", splunk.SOURCE),
            (o.language == "wazuh", wazuh.SOURCE),
            else_=kibana.SOURCE,
        ),
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
    excepted: Optional[Tuple[Dict[str, int], Dict[str, List[str]], int]] = None,
) -> Dict[str, Any]:
    """``checks`` after one run: Runs, and on a pass the stage's per-day hits,
    each exception's per-day matches and, when the run fetched rows, how many
    it dropped and which of its fields none of them carried (``seen``), and
    what they dropped together (``exceptions_seen``): all of a run's rows
    dropped means the copy raised nothing (#3994)."""
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
            matched, absent, rows = excepted
            counts = dict(checks.get("exceptions") or {})
            for eid, n in matched.items():
                entry = dict(counts.get(eid) or {})
                per_day = dict(entry.get("days") or {})
                per_day[day] = int(per_day.get(day) or 0) + n
                entry["days"] = _trimmed(per_day, day)
                if rows:  # a run with no rows teaches nothing: keep the last one's
                    entry["seen"] = {
                        "rows": rows,
                        "matched": n,
                        "missing": absent.get(eid) or [],
                        "at": end.isoformat(),
                    }
                counts[eid] = entry
            out["exceptions"] = counts
            if rows:
                out["exceptions_seen"] = {
                    "rows": rows,
                    "matched": sum(matched.values()),
                    "ids": [int(e) for e in matched],
                    "at": end.isoformat(),
                }
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
    excepted: Optional[Dict[int, tuple]] = None,
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


def _week(now: datetime, days: int = DEFAULT_LIMITS.shadow_days) -> List[str]:
    return [(now - timedelta(days=i)).date().isoformat() for i in range(days)]


def _due(
    session: Any,
    copy: Copy,
    now: datetime,
    live_24h: int,
    limits: RolloutLimits = DEFAULT_LIMITS,
) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    """(stage, reason, proof) of the move due for ``copy``, or None.

    ``limits`` are the stored rollout limits; the sweep reads them once per run.
    """
    runs = copy.checks.get("runs") or {}
    sees_check = copy.checks.get("sees") or {}
    seen = {
        k: sees_check.get(k) for k in ("result", "reason", "data_source", "checked_at")
    }
    since = copy.stage_at.isoformat()
    lint = copy.checks.get("lint") or {}
    if lint.get("result") == "fail":  # a version sync never got to turn off
        from core.detections.inventory.sync import _lint_off

        return ("off", *_lint_off(lint))
    if copy.stage == "shadow":
        failures = int(runs.get("failures") or 0)
        if failures >= OFF_AFTER_FAILURES:
            return (
                "off",
                f"{failures} failed runs in a row",
                {"failures": failures, "last_reason": runs.get("reason")},
            )
        if copy.held:  # a person's Revert stands until a person releases it
            return None
        if now - copy.stage_at < timedelta(days=limits.shadow_days):
            return None
        week = _week(now, limits.shadow_days)
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
        if any(v["hits"] > limits.max_hits_per_day for v in days.values()):
            return None
        if seen.get("result") != "pass":  # its data must be shown to arrive
            return None
        if not _linted(copy):  # no result yet: the next sync lints it
            return None
        if live_24h >= limits.live_per_day:
            return None
        return (
            "live",
            f"{limits.shadow_days} days in shadow, every day's runs passed, no day "
            f"above {limits.max_hits_per_day} hits, Sees and lint passed",
            {
                "shadow_since": since,
                "days": days,
                "max_hits_per_day": max(v["hits"] for v in days.values()),
                "live_rows_last_24h": live_24h,
                "sees": seen,
                "lint": lint,
            },
        )
    # live: only what happened since it went live counts.
    start = copy.stage_at.date().isoformat()
    hit_days = {
        d: n
        for d, n in ((copy.checks.get("live") or {}).get("days") or {}).items()
        if d >= start
    }
    loud = {d: n for d, n in hit_days.items() if n > limits.max_hits_per_day}
    if loud:
        return (
            "shadow",
            f"a live day above {limits.max_hits_per_day} hits",
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


def _asks_first() -> bool:
    """Whether the fleet is on Assist: a promotion or deploy PR waits for a
    person. An unreadable ceiling asks too; ``_stopped`` already holds that."""
    from core.response.ceiling import REVERSIBLE, effective_act, try_read_stored_ceiling

    held = try_read_stored_ceiling()
    return held is None or not effective_act(held[0], REVERSIBLE)


def sweep(now: datetime, report: RolloutReport) -> None:
    """Write a stage row for each move due, unless Stop is set. Under Assist a
    promotion to live is put to a person instead, and no deploy PR opens."""
    from core.detections.inventory.sync import _insert_stages, _stage_row

    copies = [c for c in _copies() if c.stage in ("shadow", "live")]
    # A handed-off copy keeps its stage: Vigil no longer runs it.
    skip = handed_off([c.rule_id for c in copies])
    copies = [c for c in copies if c.rule_id not in skip]
    stopped = _stopped()
    asks = not stopped and _asks_first()
    limits = read_limits()  # every run, so an edit shows at the next sweep
    targets = _deploy_targets()
    if targets.get("splunk"):
        # Off disk before the transaction: a read must not roll back stage rows.
        _read_splunk_files([c for c in copies if c.stage in ("shadow", "live")])
    with get_db_manager().session_scope() as session:
        live_24h = session.scalar(
            select(func.count())
            .select_from(DetectionRuleStage)
            .where(
                DetectionRuleStage.stage == "live",
                DetectionRuleStage.at >= now - timedelta(hours=24),
            )
        )
        rows, live = [], [c for c in copies if c.stage == "live"]
        asking: List[Tuple[Copy, str, Dict[str, Any]]] = []
        for copy in copies:
            due = _due(session, copy, now, live_24h, limits)
            if due is None:
                continue
            stage, reason, proof = due
            if stopped:
                report.held += 1
                continue
            if asks and stage == "live":
                asking.append((copy, reason, proof))
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
                live.append(copy)
            elif copy.stage == "live":
                live.remove(copy)
        if rows:
            _insert_stages(session, rows, {c.rule_id: c.stage for c in copies})
    if asking:
        # After the transaction: a request writes the audit trail in sessions of
        # its own, which must not wait on this one's trail lock.
        from core.detections.inventory import rollout_approval

        for copy, reason, proof in asking:
            if rollout_approval.request_promotion(copy, reason, proof):
                report.asked.append(f"{copy.rule_id} v{copy.version}")
    # After the stage rows commit: a PR that fails leaves the promotion standing,
    # and the next sweep is its retry.
    if stopped:
        return
    if asks:
        # Only a person's yes opens a PR: approved promotions and deploys are
        # carried out here, which also retries one that failed.
        from core.detections.inventory import rollout_approval

        rollout_approval.carry_out_approved(report)
    else:
        # A live copy promoted before lint gated it waits for a lint pass.
        _deploy([c for c in live if _linted(c)], targets, now, report)
    try:  # its own step: a failure here leaves the promotions and deploys
        _withdraw(_left_live(), targets, now, report)
    except Exception as exc:  # noqa: BLE001 -- retried next sweep
        logger.warning("detection rules: closing left-live deploy PRs failed: %s", exc)
        report.deploys.append(f"closing deploy PRs failed: {exc}")


def _linted(copy: Copy) -> bool:
    """Whether the copy's current version has a lint pass recorded."""
    return (copy.checks.get("lint") or {}).get("result") == "pass"


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
            # Bytes as stored: read_text would rewrite CRLF line ends.
            copy.toml = (root / copy.path).read_bytes().decode() if copy.path else ""
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "detection rules: overlay file of rule %s unread: %s", copy.rule_id, exc
            )
        if not copy.toml:
            logger.warning(
                "detection rules: rule %s has no overlay file; no deploy PR",
                copy.rule_id,
            )


DEPLOY_TIMEOUT = 20.0
# After a failed write, how long to look again for a PR a raced call opened.
RACE_WAITS = (0.0, 1.0, 2.0)

# (pattern on the recorded error, what a person can do about it), first match wins.
_PLAIN = (
    (
        r"no GitHub token",
        "No GitHub token. Add one to the GitHub integration in Settings.",
    ),
    (
        r"only GitHub",
        "Not a GitHub repo. Choose a github.com or GitHub Enterprise repo.",
    ),
    (
        r": 401\b",
        "Token not accepted. Check the GitHub integration's token.",
    ),
    (
        r": 403\b",
        "Token can't write here. Give it access to contents and pull requests.",
    ),
    (
        r": 404\b",
        "Repo not found. Check the deploy target's URL.",
    ),
    (
        r": 422\b",
        "GitHub refused the PR. Try again; if it repeats, check the repo.",
    ),
    (r": 5\d\d\b", "GitHub error. Try again."),
    (
        r"Timeout|ConnectError|could not be reached|Name or service",
        "GitHub didn't answer. Try again.",
    ),
)


def plain_deploy_error(error: str) -> str:
    """What a person can do about a recorded deploy error; the raw line stays alongside."""
    for pattern, said in _PLAIN:
        if re.search(pattern, error or ""):
            return said
    return "Couldn't open the PR. Try again."


def _deploy(
    copies: List[Copy],
    targets: Mapping[str, Dict[str, Any]],
    now: datetime,
    report: RolloutReport,
    actor: str = "system",
) -> Dict[int, Dict[str, Any]]:
    """Open a PR for each live copy whose format has a target and whose current
    version has none recorded yet (nor its file already on the default branch);
    record each outcome in checks['deploy'], with a config-trail row as
    ``actor``, and return them by rule id."""
    pending = [
        c
        for c in copies
        if targets.get(c.language)
        and c.toml  # a Splunk copy whose file went unread has nothing to add
        and _deploy_due(c)
    ]
    outcomes: Dict[int, Dict[str, Any]] = {}
    if not pending:
        return outcomes
    from core.detections.detection_rules_service import (
        deploy_host_refused,
        deploy_subdirectory_refused,
    )
    from core.secrets import get_secret

    with get_db_manager().session_scope() as session:
        excepted = {c.rule_id: exceptions.listed(session, c.rule_id) for c in pending}
    token = get_secret("GITHUB_TOKEN")
    for copy in pending:
        if _stopped():  # Stop set mid-pass: the rest wait for the sweep after it lifts
            return outcomes
        target = targets[copy.language]
        outcome: Dict[str, Any] = {"version": copy.version, "at": now.isoformat()}
        try:
            # Again here, not only when the target was set: a hand-edited file
            # must not send the GitHub token to a GitLab or Bitbucket host, nor
            # write outside the repo through a '..' subdirectory.
            refused = deploy_host_refused(target) or deploy_subdirectory_refused(target)
            if refused:
                raise RuntimeError(f"{target.get('git_url')}: {refused}")
            if not token:
                raise RuntimeError(
                    "no GitHub token: set the github integration's token"
                )
            done = _open_pr(
                copy,
                target,
                _pr_body(copy, excepted[copy.rule_id]),
                token,
                reopen=_withdrawn_by_vigil(_deployed(copy)),
            )
            superseded = done.pop("superseded", [])
            outcome.update(done)
            landed = done.get("pr_url") or done.get("in_repo")
            for old in superseded:
                _record_superseded(copy, old, landed, now)
            report.deploys.append(f"{copy.rule_id} v{copy.version}: {landed}")
            if done.get("supersede_error"):
                # The new version's PR stands; closing an older one is retried next sweep.
                report.deploys.append(
                    f"{copy.rule_id} v{copy.version}: closing an older PR failed: "
                    f"{done['supersede_error']}"
                )
                logger.warning(
                    "detection rules: closing rule %s's older deploy PRs failed: %s",
                    copy.rule_id,
                    done["supersede_error"],
                )
        except Exception as exc:  # noqa: BLE001 -- the promotion already landed
            outcome["error"] = (str(exc) or type(exc).__name__)[:500]
            report.deploys.append(f"{copy.rule_id} v{copy.version}: {outcome['error']}")
            logger.warning(
                "detection rules: deploy PR for rule %s v%s to %s failed: %s",
                copy.rule_id,
                copy.version,
                target.get("git_url"),
                outcome["error"],
            )
        try:
            outcomes[copy.rule_id] = _record_deploy(copy, outcome, actor)
        except Exception as exc:  # noqa: BLE001 -- nothing written; redone next pass
            what = outcome.get("pr_url") or outcome.get("in_repo") or outcome["error"]
            logger.error(
                "detection rules: deploy of rule %s v%s not recorded: %s",
                copy.rule_id,
                copy.version,
                exc,
            )
            why = f"{what}: not recorded ({exc}); the next sweep records it"
            outcomes[copy.rule_id] = {**outcome, "error": why[:500]}
    return outcomes


def _deploy_due(copy: Copy) -> bool:
    """Whether the sweep still has work for this copy's current version: no PR
    or in_repo recorded yet (one recorded for an earlier version doesn't count;
    the new one gets its own), closing an older version's PR failed, or Vigil
    closed this version's PR when it left live and it is live again."""
    done = _deployed(copy) or {}
    return (
        not {"pr_url", "in_repo"} & set(done)
        or bool(done.get("supersede_error"))
        or _withdrawn_by_vigil(done)
    )


def _withdrawn_by_vigil(deploy: Optional[Mapping[str, Any]]) -> bool:
    """Whether Vigil itself closed this deploy record's PR when the rule left live."""
    withdrawn = (deploy or {}).get("withdrawn")
    return isinstance(withdrawn, dict) and withdrawn.get("closed") is True


def _api_of(git_url: str) -> Tuple[str, str]:
    """(REST API base, "owner/repo") of a github.com or GitHub Enterprise Server URL."""
    from urllib.parse import urlsplit

    from core.detections.detection_rules_service import _repo_key

    host, _, repo = _repo_key(git_url).partition("/")
    if host == "github.com":
        return "https://api.github.com", repo
    parts = urlsplit(git_url.strip())
    # An http(s) URL keeps its scheme and port; ssh and scp forms reach https.
    if parts.scheme in ("http", "https"):
        origin = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"
    else:
        origin = f"https://{host}"
    return f"{origin}/api/v3", repo


def _ok(resp: Any) -> Any:
    if resp.is_error:
        try:
            why = resp.json().get("message")
        except ValueError:
            why = resp.text[:200]
        raise RuntimeError(
            f"{resp.request.method} {resp.request.url.path}: {resp.status_code} {why}"
        )
    return resp


def _pr_title(copy: Copy) -> str:
    return f"Add Vigil rule {copy.name} ({copy.key})"


def _pr_body(copy: Copy, excepted: List[Dict[str, Any]]) -> str:
    """What the rule is, and its active exceptions; the rule file has none."""
    splunk = copy.language == "splunk"
    body = (
        f"Vigil detection rule {copy.rule_id} v{copy.version}, {copy.name!r} "
        f"({'ESCU id' if splunk else 'rule_id'} {copy.key}), is live in Vigil. "
        f"This adds its {'ESCU YAML' if splunk else 'TOML'} exactly as Vigil "
        f"stores it ({copy.path or 'its overlay file'})."
    )
    if excepted:
        lines = "\n".join(
            f"- `{json.dumps(r['match'], sort_keys=True)}`: {r['note']}"
            for r in excepted
        )
        body += (
            "\n\nCustomer-specific exceptions, not in the rule file: Vigil applied "
            "them to this rule's hits and stops once the SIEM runs it, so carry "
            "them into the SIEM's own exception mechanism (for example a Kibana "
            f"exception list).\n{lines}"
        )
    return body


def _open_pr(
    copy: Copy,
    target: Mapping[str, Any],
    body: str,
    token: str,
    reopen: bool = False,
) -> Dict[str, Any]:
    """{"pr_url"} of the pull request adding ``copy``'s file to ``target``, or
    {"in_repo"} when the default branch already holds it byte for byte. Reuses
    the branch or PR an earlier attempt left, so a crash between calls opens no
    duplicate. Either way older versions' open PRs are closed (``superseded``);
    a failure there is ``supersede_error`` beside the result, not instead of it.
    ``reopen``: Vigil closed this version's PR when the rule left live, so a
    closed, unmerged one is reopened ({"reopened": True}) rather than left as a
    person's answer."""
    import base64
    import posixpath
    from urllib.parse import quote

    import httpx

    from core.detections.detection_rules_service import (
        deploy_host_refused,
        deploy_subdirectory_refused,
    )

    # Last guard before the token leaves: whoever calls this, an unmarked
    # non-github.com host, or a subdirectory that climbs out of the repo, gets
    # no request at all.
    refused = deploy_host_refused(target) or deploy_subdirectory_refused(target)
    if refused:
        raise RuntimeError(f"{target.get('git_url')}: {refused}")
    api, repo = _api_of(str(target.get("git_url") or ""))
    owner = repo.partition("/")[0]
    branch = f"vigil/rule-{copy.rule_id}-v{copy.version}"
    ext = "yml" if copy.language == "splunk" else "toml"
    name = posixpath.basename(copy.path or "") or f"vigil-rule-{copy.rule_id}.{ext}"
    path = posixpath.join(str(target.get("subdirectory") or "").strip("/"), name)
    data = copy.toml.encode()
    r = f"/repos/{repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(
        base_url=api, headers=headers, timeout=DEPLOY_TIMEOUT, follow_redirects=True
    ) as http:
        # Any state: a PR a person closed is their answer, not a reason to reopen.
        pulls = _ok(
            http.get(f"{r}/pulls", params={"head": f"{owner}:{branch}", "state": "all"})
        ).json()
        if pulls:
            pr = pulls[0]
            if pr.get("state") != "open" and reopen and not pr.get("merged_at"):
                note = f"Reopened by Vigil: rule {copy.name} v{copy.version} is live again."
                comments = f"{r}/issues/{pr['number']}/comments"
                said = _ok(http.get(comments, params={"per_page": 100})).json()
                # Once per reopening: a retry finds it already the latest comment.
                if not said or said[-1].get("body") != note:
                    _ok(http.post(comments, json={"body": note}))
                _ok(http.patch(f"{r}/pulls/{pr['number']}", json={"state": "open"}))
                return {
                    "pr_url": pr["html_url"],
                    "reopened": True,
                    **_supersede(http, r, owner, copy, _by_pr(copy, pr)),
                }
            # A PR a person closed stays their answer: nothing older is closed for it.
            if pr.get("state") != "open":
                return {"pr_url": pr["html_url"]}
            return {
                "pr_url": pr["html_url"],
                **_supersede(http, r, owner, copy, _by_pr(copy, pr)),
            }
        base = _ok(http.get(r)).json()["default_branch"]
        if _same(http, f"{r}/contents/{quote(path)}", base, data)[1]:
            in_repo = f"{path} on {base}"  # a PR would have no commits
            # The file landing outside Vigil's PR still makes older versions' PRs stale.
            note = (
                f"Superseded: rule {copy.name} v{copy.version} is already in {in_repo}"
            )
            return {"in_repo": in_repo, **_supersede(http, r, owner, copy, note)}
        ref = http.get(f"{r}/git/ref/heads/{quote(branch)}")
        if ref.status_code == 404:
            sha = _ok(http.get(f"{r}/git/ref/heads/{quote(base)}")).json()["object"]
            made = http.post(
                f"{r}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": sha["sha"]},
            )
            # 422: another click or sweep made the branch first; carry on with it.
            if made.status_code != 422:
                _ok(made)
        else:
            _ok(ref)
        put: Dict[str, Any] = {
            "message": _pr_title(copy),
            "content": base64.b64encode(data).decode(),
            "branch": branch,
        }
        sha, same = _same(http, f"{r}/contents/{quote(path)}", branch, data)
        if sha:
            put["sha"] = sha  # the file is there: update it in place
        try:
            if not same:
                _ok(http.put(f"{r}/contents/{quote(path)}", json=put))
            pr = _ok(
                http.post(
                    f"{r}/pulls",
                    json={
                        "title": _pr_title(copy),
                        "head": branch,
                        "base": base,
                        "body": body,
                    },
                )
            ).json()
        except RuntimeError:
            # A raced click or sweep wrote the file or opened the PR first: its PR
            # is this one. Look again briefly, as it may land a moment later.
            for wait in RACE_WAITS:
                time.sleep(wait)
                found = http.get(
                    f"{r}/pulls", params={"head": f"{owner}:{branch}", "state": "all"}
                )
                if not found.is_error and found.json():
                    return {"pr_url": found.json()[0]["html_url"]}
            raise
        return {
            "pr_url": pr["html_url"],
            **_supersede(http, r, owner, copy, _by_pr(copy, pr)),
        }


def _by_pr(copy: Copy, new: Mapping[str, Any]) -> str:
    return f"Superseded by #{new['number']} (rule {copy.name} v{copy.version})"


def _supersede(http: Any, r: str, owner: str, copy: Copy, note: str) -> Dict[str, Any]:
    """Close each still-open PR Vigil opened for an earlier version of
    ``copy``'s rule, commenting ``note`` on it once (a retry finds it already
    there). Only Vigil's own branch for that rule and version, in this repo,
    and never a merged PR. {"superseded": [{version, pr_url}] closed, and
    "supersede_error" when a lookup, comment or close failed}; one failure
    doesn't stop the others."""
    repo = r.removeprefix("/repos/")
    closed: List[Dict[str, Any]] = []
    errors: List[str] = []
    for version in range(1, copy.version):
        branch = f"vigil/rule-{copy.rule_id}-v{version}"
        try:
            pulls = _ok(
                http.get(
                    f"{r}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}
                )
            ).json()
        except Exception as exc:  # noqa: BLE001 -- the new version's result stands
            errors.append(str(exc) or type(exc).__name__)
            continue
        for old in pulls:
            head = old.get("head") or {}
            if (
                old.get("state") != "open"
                or old.get("merged_at")
                or head.get("ref") != branch
                or str((head.get("repo") or {}).get("full_name", "")).lower()
                != repo.lower()
            ):
                continue
            comments = f"{r}/issues/{old['number']}/comments"
            try:
                said = _ok(http.get(comments, params={"per_page": 100})).json()
                if not any(c.get("body") == note for c in said):
                    _ok(http.post(comments, json={"body": note}))
                _ok(http.patch(f"{r}/pulls/{old['number']}", json={"state": "closed"}))
            except Exception as exc:  # noqa: BLE001 -- the new version's result stands
                errors.append(str(exc) or type(exc).__name__)
                continue
            closed.append({"version": version, "pr_url": old["html_url"]})
    out: Dict[str, Any] = {"superseded": closed}
    if errors:
        out["supersede_error"] = "; ".join(errors)[:500]
    return out


def _same(http: Any, url: str, ref: str, data: bytes) -> Tuple[Optional[str], bool]:
    """(blob sha or None, whether it is ``data``) of the file at ``url`` on ``ref``."""
    import base64

    have = http.get(url, params={"ref": ref})
    meta = _ok(have).json() if have.status_code != 404 else {}
    if not isinstance(meta, dict) or not meta.get("sha"):
        return None, False
    return meta["sha"], base64.b64decode(meta.get("content") or "") == data


def _record_deploy(
    copy: Copy, outcome: Dict[str, Any], actor: str = "system"
) -> Dict[str, Any]:
    """checks['deploy'] on the copy's version, merged into its checks as they are
    now, with its config-trail row in the same transaction: a trail failure
    records neither, so the next sweep re-finds the PR and records both. Returns
    the outcome left recorded: a PR a racing attempt recorded is not replaced by
    this one's error."""
    from core.detections.inventory.sync import _write_checks, trail_actor
    from core.storage.config_service import write_config_audit

    with get_db_manager().session_scope() as session:
        # Locked, so a racing attempt reads what this one wrote.
        checks = session.scalar(
            select(DetectionRuleVersion.checks)
            .where(
                DetectionRuleVersion.rule_id == copy.rule_id,
                DetectionRuleVersion.version == copy.version,
            )
            .with_for_update()
        )
        had = (checks or {}).get("deploy")
        if not (isinstance(had, dict) and had.get("version") == copy.version):
            had = None
        if had and "error" in outcome and {"pr_url", "in_repo"} & set(had):
            return had
        _write_checks(
            session,
            [(copy.rule_id, copy.version, {**(checks or {}), "deploy": outcome})],
        )
        # One row per change of outcome, not one per sweep retrying the same error.
        new = {k: v for k, v in outcome.items() if k != "at"}
        if not had or {k: v for k, v in had.items() if k != "at"} != new:
            write_config_audit(
                session,
                config_type="detection_rules",
                config_key="overlay_deploy",
                action="update",
                old_value=None,
                new_value={"rule_id": copy.rule_id, **new},
                changed_by=trail_actor(actor),
            )
    return outcome


def _record_superseded(
    copy: Copy, old: Mapping[str, Any], new_url: str, now: datetime
) -> None:
    """On the old version's checks['deploy']: its PR was closed for ``new_url``,
    the new version's PR, or where its file already is when it landed in_repo."""
    from core.detections.inventory.sync import _write_checks

    with get_db_manager().session_scope() as session:
        checks = session.scalar(
            select(DetectionRuleVersion.checks).where(
                DetectionRuleVersion.rule_id == copy.rule_id,
                DetectionRuleVersion.version == old["version"],
            )
        )
        if checks is None:
            return
        deploy = {
            **(checks.get("deploy") or {}),
            "pr_url": old["pr_url"],
            "superseded_by": new_url,
            "superseded_at": now.isoformat(),
        }
        _write_checks(
            session, [(copy.rule_id, old["version"], {**checks, "deploy": deploy})]
        )


@dataclass
class Withdrawal:
    """A version's deploy PR, recorded open, of a rule that is no longer live."""

    rule_id: int
    version: int
    name: str
    language: str
    deploy: Dict[str, Any]  # the version's checks['deploy'] as recorded
    stage: str  # where the rule is now
    actor: str  # who moved it there: 'system' is Vigil
    reason: str


def _left_live() -> List[Withdrawal]:
    """Each overlay copy version whose deploy PR is recorded open (not
    superseded, not withdrawn) while the copy's current stage is not live,
    with the current stage row's stage, actor and reason."""
    s = DetectionRuleStage
    current = (
        select(s.rule_id, s.stage, s.actor, s.reason)
        .distinct(s.rule_id)
        .order_by(s.rule_id, s.at.desc(), s.id.desc())
        .subquery()
    )
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(
                DetectionRule.id,
                DetectionRule.name,
                DetectionRule.language,
                DetectionRuleVersion.version,
                DetectionRuleVersion.checks["deploy"],
                current.c.stage,
                current.c.actor,
                current.c.reason,
            )
            .join(
                DetectionRuleVersion, DetectionRuleVersion.rule_id == DetectionRule.id
            )
            .join(current, current.c.rule_id == DetectionRule.id)
            .where(
                DetectionRule.source == readers.OVERLAY_SOURCE,
                DetectionRule.language.in_(("elastic", "splunk")),
                current.c.stage != "live",
                DetectionRuleVersion.checks.has_key("deploy"),
            )
            .order_by(DetectionRule.id, DetectionRuleVersion.version)
        ).all()
    return [
        Withdrawal(rid, version, name, lang, deploy, stage, actor, reason or "")
        for rid, name, lang, version, deploy, stage, actor, reason in rows
        if isinstance(deploy, dict)
        and deploy.get("version") == version
        and deploy.get("pr_url")
        and not {"superseded_by", "withdrawn"} & set(deploy)
    ]


def _withdraw_note(w: Withdrawal) -> str:
    who = "Vigil" if w.actor == "system" else w.actor
    return (
        f"Closed by Vigil: rule {w.name} v{w.version} is no longer live. "
        f"{who} moved it to {w.stage}: {w.reason or 'no reason given'}"
    )


def _withdraw(
    items: List[Withdrawal],
    targets: Mapping[str, Dict[str, Any]],
    now: datetime,
    report: RolloutReport,
) -> None:
    """Close each left-live version's deploy PR, and record what came of it."""
    pending = [w for w in items if targets.get(w.language)]
    if not pending:
        return
    from core.detections.detection_rules_service import deploy_host_refused
    from core.secrets import get_secret

    token = get_secret("GITHUB_TOKEN")
    for w in pending:
        if _stopped():  # Stop set mid-pass: the rest wait for the sweep after it lifts
            return
        target = targets[w.language]
        url = w.deploy["pr_url"]
        try:
            refused = deploy_host_refused(target)
            if refused:
                raise RuntimeError(f"{target.get('git_url')}: {refused}")
            if not token:
                raise RuntimeError(
                    "no GitHub token: set the github integration's token"
                )
            closed, note = _close_pr(w, target, token)
        except Exception as exc:  # noqa: BLE001 -- retried next sweep
            error = (str(exc) or type(exc).__name__)[:500]
            report.deploys.append(
                f"{w.rule_id} v{w.version}: closing {url} failed: {error}"
            )
            logger.warning(
                "detection rules: closing rule %s v%s's deploy PR %s failed: %s",
                w.rule_id,
                w.version,
                url,
                error,
            )
            _record_withdrawn(w, {"withdraw_error": error})
            continue
        withdrawn = {
            "at": now.isoformat(),
            "stage": w.stage,
            "actor": w.actor,
            "reason": w.reason,
            "closed": closed,
        }
        if note:
            withdrawn["note"] = note
        report.deploys.append(
            f"{w.rule_id} v{w.version}: "
            + (f"closed {url}" if closed else f"{url} not closed: {note}")
        )
        _record_withdrawn(w, {"withdrawn": withdrawn})


def _close_pr(w: Withdrawal, target: Mapping[str, Any], token: str) -> Tuple[bool, str]:
    """(True, "") once the PR is closed with the note; (False, why) for a PR
    left as it is: merged, already closed, or not in the target's repo."""
    import httpx

    from core.detections.detection_rules_service import _repo_key

    url = str(w.deploy["pr_url"])
    m = re.fullmatch(r"(.+)/pull/(\d+)/?", url)
    api, repo = _api_of(str(target.get("git_url") or ""))
    if not m or _repo_key(m[1]) != _repo_key(str(target.get("git_url") or "")):
        # The token goes only to the target's repo, never wherever a URL points.
        return False, "not in the deploy target's repo"
    r, number = f"/repos/{repo}", int(m[2])
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(
        base_url=api, headers=headers, timeout=DEPLOY_TIMEOUT, follow_redirects=True
    ) as http:
        pr = _ok(http.get(f"{r}/pulls/{number}")).json()
        if pr.get("merged_at"):
            return False, "merged before the rule left live"
        if pr.get("state") != "open":
            return False, "already closed"  # a person's answer: never reopened
        note = _withdraw_note(w)
        comments = f"{r}/issues/{number}/comments"
        said = _ok(http.get(comments, params={"per_page": 100})).json()
        if not any(c.get("body") == note for c in said):  # once, across retries
            _ok(http.post(comments, json={"body": note}))
        _ok(http.patch(f"{r}/pulls/{number}", json={"state": "closed"}))
    return True, ""


def _record_withdrawn(w: Withdrawal, change: Dict[str, Any]) -> None:
    """``change`` (withdrawn, or withdraw_error) merged into the version's
    checks['deploy'] as it is now, with a config-trail row for a close."""
    from core.detections.inventory.sync import _write_checks
    from core.storage.config_service import write_config_audit

    with get_db_manager().session_scope() as session:
        checks = session.scalar(
            select(DetectionRuleVersion.checks)
            .where(
                DetectionRuleVersion.rule_id == w.rule_id,
                DetectionRuleVersion.version == w.version,
            )
            .with_for_update()
        )
        checks = checks or {}
        deploy = checks.get("deploy")
        if not isinstance(deploy, dict) or deploy.get("pr_url") != w.deploy["pr_url"]:
            return  # replaced since it was read: the next sweep reads it afresh
        deploy = {k: v for k, v in deploy.items() if k != "withdraw_error"}
        _write_checks(
            session,
            [(w.rule_id, w.version, {**checks, "deploy": {**deploy, **change}})],
        )
        if "withdrawn" in change:
            write_config_audit(
                session,
                config_type="detection_rules",
                config_key="overlay_deploy",
                action="update",
                old_value=None,
                new_value={
                    "rule_id": w.rule_id,
                    "version": w.version,
                    "pr_url": w.deploy["pr_url"],
                    "withdrawn": change["withdrawn"],
                },
                changed_by="system",
            )


def _deployed(copy: Copy) -> Optional[Dict[str, Any]]:
    """The deploy outcome recorded on the copy's current version, if any."""
    out = copy.checks.get("deploy")
    return out if isinstance(out, dict) and out.get("version") == copy.version else None


def overlay_only() -> List[Dict[str, Any]]:
    """Live overlay copies no SIEM rule runs: only Vigil detects with these, so
    Stop halts their detection. Each says whether its format has a deploy
    target Vigil deploys to (``target_refused``: why not, for one that isn't
    GitHub) and what its deploy PR came to (``deploy``: pr_url, in_repo, or
    error with a plain ``reason``, from checks['deploy'] on its current
    version; None before one)."""
    from core.detections.detection_rules_service import deploy_host_refused

    live = [c for c in _copies() if c.stage == "live" and c.language in VIGIL_RUNS]
    gone = handed_off([c.rule_id for c in live])
    targets = _deploy_targets()
    rows = []
    for c in live:
        if c.rule_id in gone:
            continue
        target = targets.get(c.language)
        # A target Vigil won't deploy to (not GitHub) is no target for the button.
        refused = deploy_host_refused(target) if target else ""
        deploy = _deployed(c)
        if deploy and deploy.get("error"):
            deploy = {**deploy, "reason": plain_deploy_error(deploy["error"])}
        rows.append(
            {
                "id": c.rule_id,
                "name": c.name,
                "language": c.language,
                "deploy_target": bool(target) and not refused,
                "target_refused": refused or None,
                "deploy": deploy,
            }
        )
    return rows


def deploy_overlay_only(
    rule_ids: Optional[List[int]] = None,
    actor: str = "system",
    approver: bool = False,
) -> Dict[str, Any]:
    """Open the deploy PR now, as the sweep would, for live overlay-only copies
    (these ``rule_ids``, or all) whose format has a GitHub deploy target and
    with no PR opened yet; one that failed is tried again. Says what each came
    to (``opened``: pr_url or in_repo; ``failed``: error), which now wait for a
    person (``asked``) and why each other one was skipped. Nothing is opened
    while Stop is set. Under Assist each one waits for an approval unless
    ``approver``: the person deploying may approve, or a person already did.
    Each outcome's config-trail row names ``actor``."""
    from core.detections.detection_rules_service import deploy_host_refused

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
    todo = []
    for copy in wanted:
        target = targets.get(copy.language)
        done = _deployed(copy) or {}
        if not target:
            skipped[copy.rule_id] = f"no {copy.language} deploy target is set"
        elif deploy_host_refused(target):
            skipped[copy.rule_id] = deploy_host_refused(target)
        elif done.get("pr_url"):
            skipped[copy.rule_id] = (
                f"its pull request is already open: {done['pr_url']}"
            )
        elif done.get("in_repo"):
            skipped[copy.rule_id] = f"it is already in the repo: {done['in_repo']}"
        elif not copy.toml:
            skipped[copy.rule_id] = "its overlay file could not be read"
        elif not _linted(copy):
            skipped[copy.rule_id] = "its current version has not passed lint"
        else:
            todo.append(copy)
    if todo and _stopped():
        skipped.update(
            {c.rule_id: "Vigil is stopped; it opens no pull request" for c in todo}
        )
        todo = []
    asked: List[Dict[str, Any]] = []
    if todo and not approver and _asks_first():
        from core.detections.inventory import rollout_approval

        asked = [
            {
                "rule_id": c.rule_id,
                "action_id": rollout_approval.request_deploy(c, actor),
            }
            for c in todo
        ]
        todo = []
    outcomes = _deploy(
        todo, targets, datetime.now(timezone.utc), RolloutReport(), actor
    )
    return {
        "asked": asked,
        "opened": [
            {
                "rule_id": r,
                **{k: o[k] for k in ("pr_url", "in_repo", "supersede_error") if k in o},
            }
            for r, o in outcomes.items()
            if "error" not in o
        ],
        "failed": [
            {
                "rule_id": r,
                "error": o["error"],
                "reason": plain_deploy_error(o["error"]),
            }
            for r, o in outcomes.items()
            if "error" in o
        ],
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
        ("wazuh", _run_wazuh_copies),
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
) -> Tuple[Dict[int, tuple], Dict[int, tuple]]:
    """Results with the documents an active exception matches dropped, and by
    rule id (matches per exception id, each one's fields absent from every
    fetched row, rows fetched). The total drops by the same number."""
    ran = [rid for rid, (_, _, error) in results.items() if not error]
    excepted = exceptions.active(ran)
    out, counts = dict(results), {}
    for rid, rules in excepted.items():
        total, docs, error = results[rid]
        kept, matched, absent = exceptions.split(docs, rules)
        counts[rid] = (matched, absent, len(docs))
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


async def _wazuh_query(
    service: Any, copy: Copy, start: datetime, end: datetime
) -> Tuple[int, List[Dict[str, Any]]]:
    query = wazuh_shadow.translate(copy.toml).query
    window = {"@timestamp": {"gte": start.isoformat(), "lt": end.isoformat()}}
    return await service.dsl_search(wazuh_shadow.ARCHIVES, query, window, MAX_DOCS)


async def _archive_events(
    service: Any, per_hour: int, report: RolloutReport
) -> Optional[bool]:
    """Whether the archive has had an event in the Sees window; None when the
    budget is spent. The index listing is metadata and spends nothing."""
    why, exists_ = await wazuh_sees.indices(service, (wazuh_shadow.ARCHIVES,))
    if why:
        raise RuntimeError(why)
    if not exists_[wazuh_shadow.ARCHIVES]:
        return False
    if not search_budget.spend(INTEGRATION, per_hour, "overlay:wazuh-archives"):
        return None
    report.searched += 1
    return await service.any_event_since(wazuh_shadow.ARCHIVES, "@timestamp", sees.DAYS)


def _write_sees(copies: List[Tuple[Copy, str, str]], now: datetime) -> int:
    """checks['sees'] per (copy, result, reason): the archive is the data source."""
    results = {
        (c.rule_id, c.version): {
            "result": result,
            "reason": reason,
            "data_source": wazuh_shadow.ARCHIVES,
            "checked_at": now.isoformat(),
        }
        for c, result, reason in copies
    }
    return sees.write_check("sees", results, source=readers.OVERLAY_SOURCE)


def _run_wazuh_copies(copies: List[Copy], now: datetime, report: RolloutReport) -> None:
    """Shadow runs of Wazuh copies over the archive, on elastic-siem's indexer and
    budget. A live copy is the manager's to run. A copy whose rule cannot be
    translated, or whose archive is off, is not validated: its runs read unknown
    with the reason and its Sees is unknown, so it never promotes."""
    shadow = [c for c in copies if c.stage == "shadow"]
    plan = {c.rule_id: wazuh_shadow.translate(c.toml) for c in shadow}
    blocked = [(c, plan[c.rule_id].why) for c in shadow if plan[c.rule_id].why]
    ready = [c for c in shadow if not plan[c.rule_id].why]

    def unvalidated(why: str) -> None:
        every = blocked + [(c, why) for c in ready]
        report.written += _write_unrunnable(every, now)
        report.written += _write_sees([(c, "unknown", w) for c, w in every], now)

    why, config = wazuh_sees.indexer()
    if why:
        unvalidated(f"{wazuh_shadow.CANT}{why}")
        report.skipped.append(why)
        return
    per_hour = config.get("max_searches_per_hour")
    per_hour = sees.DEFAULT_PER_HOUR if per_hour is None else max(int(per_hour), 0)
    service = kibana.service_of(config)

    async def _go() -> Tuple[str, Dict[int, tuple]]:
        try:
            if not ready:
                return "ran", {}
            on = await _archive_events(service, per_hour, report)
            if on is None:
                report.deferred += len(ready)
                return "deferred", {}
            if not on:
                return "off", {}
            ran = await _run_all(
                INTEGRATION, service, _wazuh_query, ready, per_hour, now, report
            )
            return "ran", ran
        finally:
            await service.close()

    try:
        state, results = asyncio.run(_go())
    except Exception as exc:  # noqa: BLE001 -- the copies stay as they were
        report.error = report.error or f"wazuh: {type(exc).__name__}: {exc}"
        return
    if state == "deferred":
        return
    if state == "off":
        unvalidated(wazuh_shadow.ARCHIVES_OFF)
        return
    report.written += _write_unrunnable(blocked, now)
    ok = f"{wazuh_shadow.ARCHIVES} has events in {sees.DAYS}d"
    report.written += _write_sees(
        [(c, "unknown", w) for c, w in blocked]
        + [(c, "pass", ok) for c in ready if c.rule_id in results],
        now,
    )
    _finish(ready, results, now, report)
