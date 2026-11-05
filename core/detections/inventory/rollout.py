"""Rollout: Vigil runs its Elastic overlay copies and moves them shadow -> live -> back.

Each daemon sync tick, after ``sync_repos``, every active Elastic overlay copy
whose current stage (its latest stage row) is shadow or live is run against the
elastic-siem Elasticsearch over a window that runs ``LAG`` behind now: from
where its last successful run stopped (or an hour before that) up to now - LAG,
so an event stamped earlier but indexed late still lands in a window not yet
searched, and is counted once. EQL goes through ``_eql/search``, ES|QL through
``_query``, Lucene through ``_search``. Each run spends one elastic-siem search (key ``overlay:<rule id>``),
least recently run first; once the hour's budget is spent the rest wait.

A run writes checks['runs'] on the latest version (result, reason, last
successful run, consecutive failures, per-day pass/fail), recording a changed
result through ``_record_changes`` so ``rule_health`` flags a broken copy. Its
hits add to checks['shadow'] or checks['live'] per-day counts; a shadow copy
also keeps a small sample, a live copy's hits become findings for the caller to
queue. A run fetches at most ``MAX_DOCS`` documents (its count is the full
total): a live copy past that in one run is over the daily hit limit and
reverts in the same tick.

The sweep then writes a stage row (actor 'system', proof holding the numbers)
for each move that is due: shadow -> live after a quiet, passing week with
checks['sees'] pass (at most ``LIVE_PER_DAY`` a day across all rules), live ->
shadow on noise, a loud day or a Sees fail (its data stopped), shadow -> off
after ``OFF_AFTER_FAILURES`` failed runs in a row. No stage row is written
while Stop is set, or while the ceiling cannot be read. A move to live, when a
git source is marked ``deploy_target`` for elastic, queues one
``agent-run-mitre_analyst`` run that opens a pull request adding the rule there.

A copy an active Kibana rule now runs (one linked to the overlay rule, or to a
repo rule with its rule_id) is handed off: the runner skips it, its stage stays.

Nothing is written to Elasticsearch or Kibana.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy import and_, bindparam, func, insert, select, text, update

from core.detections.inventory import kibana, readers, sees
from core.detections.inventory.catalog import REPO_PREFIX
from core.integrations._base import search_budget
from core.integrations._base.ids import EXTERNAL_ID_MAX, fit_id
from core.integrations.elastic.descriptor import ELASTIC
from core.storage.connection import get_db_manager
from core.storage.models import (
    DetectionRule,
    DetectionRuleStage,
    DetectionRuleVersion,
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


def _budget_key(rule_id: int) -> str:
    # The prefix keeps it apart from Sees' "<indices>|<field>" keys.
    return f"overlay:{rule_id}"


@dataclass
class Copy:
    rule_id: int
    version: int
    name: str
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
    written: int = 0
    moves: List[str] = field(default_factory=list)
    held: int = 0  # due moves not written: Stop set or the ceiling unread
    handed_off: int = 0  # copies an active SIEM rule runs instead
    queued: List[int] = field(default_factory=list)  # intake rows for deploy PRs
    findings: List[Dict[str, Any]] = field(default_factory=list, repr=False)
    error: Optional[str] = None
    skipped: Optional[str] = None


def _toml_rule(content: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        rule = tomllib.loads(str(content.get("_content") or "")).get("rule")
    except tomllib.TOMLDecodeError:
        return {}
    return rule if isinstance(rule, dict) else {}


def _copies() -> List[Copy]:
    """Active Elastic overlay copies at their latest version, with their current stage."""
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
                DetectionRule.language == "elastic",
                DetectionRule.status == "active",
            )
            .order_by(DetectionRule.id)
        ).all()
    return [
        Copy(
            rid,
            ver,
            name,
            stage,
            at,
            checks or {},
            _toml_rule(content or {}),
            key,
            str((content or {}).get("_content") or ""),
            path,
        )
        for rid, ver, name, stage, at, checks, content, key, path in rows
    ]


LINKED = ("same_id", "known_name", "agent")  # 'declined' is not a link


def siem_runs(siem: str, rule_ids: List[int]) -> set:
    """Of these overlay rules, those an active linked SIEM copy runs.

    A ``siem`` copy (status active, so enabled) linked to the overlay rule
    itself, or to a repo rule of its language carrying its key, runs it now.
    """
    if not rule_ids:
        return set()
    overlay, repo, siem_copy = (
        DetectionRule.__table__.alias(n) for n in ("o", "r", "c")
    )
    with get_db_manager().session_scope() as session:
        return set(
            session.scalars(
                select(overlay.c.id)
                .distinct()
                .join(
                    repo,
                    and_(
                        repo.c.source.startswith(REPO_PREFIX),
                        repo.c.language == overlay.c.language,
                        repo.c.native_key == overlay.c.native_key,
                    ),
                )
                .join(siem_copy, siem_copy.c.origin_rule_id == repo.c.id)
                .where(
                    overlay.c.id.in_(rule_ids),
                    siem_copy.c.source == siem,
                    siem_copy.c.status == "active",
                    siem_copy.c.link_kind.in_(LINKED),
                )
            )
        )


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


def runnable(rule: Mapping[str, Any]) -> Optional[str]:
    """Why the runner cannot run this [rule] table, or None when it can."""
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


def _doc_id(doc: Mapping[str, Any]) -> str:
    if doc.get("_id"):
        return str(doc["_id"])
    # ES|QL rows carry no _id unless the query asks for METADATA _id.
    blob = json.dumps(doc.get("_source"), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:20]


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
) -> Dict[str, Any]:
    """``checks`` after one run: Runs, and the stage's per-day hits on a pass."""
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
    return out


def _entities(source: Mapping[str, Any]) -> Dict[str, List[str]]:
    """The few ECS fields triage keys on, when the hit has them."""

    def get(*path: str) -> Any:
        value: Any = source
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        return value if value is not None else source.get(".".join(path))

    out: Dict[str, List[str]] = {}
    for key, path in (
        ("hostnames", ("host", "name")),
        ("usernames", ("user", "name")),
        ("src_ips", ("source", "ip")),
        ("dest_ips", ("destination", "ip")),
    ):
        value = get(*path)
        if isinstance(value, (str, int)):
            out[key] = [str(value)]
    return out


def _finding(copy: Copy, doc: Mapping[str, Any], end: datetime) -> Dict[str, Any]:
    hit_id = _doc_id(doc)
    digest = hashlib.sha256(f"{copy.rule_id}:{hit_id}".encode()).hexdigest()[:32]
    source = doc.get("_source") if isinstance(doc.get("_source"), dict) else {}
    ts = copy.rule.get("timestamp_override") or "@timestamp"
    stamp = source.get(ts) if isinstance(source.get(ts), str) else end.isoformat()
    severity = str(copy.rule.get("severity") or "medium").lower()
    return {
        "finding_id": f"overlay-{digest}",
        "external_id": fit_id("", f"overlay:{copy.rule_id}:{hit_id}", EXTERNAL_ID_MAX),
        "data_source": "elastic",
        "timestamp": stamp,
        "severity": severity,
        "title": str(copy.rule.get("name") or copy.name),
        "description": str(copy.rule.get("description") or "")[:500],
        "entity_context": {
            **_entities(source),
            "detection_rule": {"rule_id": copy.rule_id, "version": copy.version},
        },
        "raw_data": dict(doc),  # becomes source_evidence.records
    }


async def _run_all(
    config: Mapping[str, Any],
    copies: List[Copy],
    per_hour: int,
    now: datetime,
    report: RolloutReport,
) -> Dict[int, Tuple[Optional[int], List[Dict], Optional[str]]]:
    """(total, docs, error) by rule id, for the copies the budget reached."""
    results: Dict[int, Tuple[Optional[int], List[Dict], Optional[str]]] = {}
    last = search_budget.last_spent(
        INTEGRATION, [_budget_key(c.rule_id) for c in copies]
    )
    never = datetime.min.replace(tzinfo=timezone.utc)
    order = sorted(copies, key=lambda c: last.get(_budget_key(c.rule_id), never))
    service = kibana.service_of(config)
    try:
        for n, copy in enumerate(order):
            if not search_budget.spend(
                INTEGRATION, per_hour, _budget_key(copy.rule_id)
            ):
                report.deferred = len(order) - n
                break
            report.searched += 1
            try:
                total, docs = await _query(service, copy, *_window(copy, now))
                results[copy.rule_id] = (total, docs, None)
            except Exception as exc:  # noqa: BLE001 -- the run reads fail
                results[copy.rule_id] = (None, [], str(exc)[:500] or type(exc).__name__)
    finally:
        await service.close()
    return results


def _write_runs(copies: List[Copy], results: Dict[int, tuple], now: datetime) -> int:
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
                checks or {}, copy.stage, now, results[rid], _window(copy, now)[1]
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
    """Copies Kibana runs now stop carrying Runs: a NULL row closes their flag."""
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
                c.rule_id, c.version, c.checks, ("runs",), last, "run by Kibana"
            )
            changes += rows
            if checks != c.checks:
                checked.append((c.rule_id, c.version, checks))
        _write_checks(session, checked)
        _record_changes(session, changes, now.isoformat())


def _write_unrunnable(copies: List[Tuple[Copy, str]], now: datetime) -> int:
    """checks['runs'] unknown for copies the runner cannot run, once per reason."""
    results = {
        (c.rule_id, c.version): {"result": "unknown", "reason": why}
        for c, why in copies
        if (c.checks.get("runs") or {}).get("reason") != why
    }
    return (
        sees.write_check("runs", results, source=readers.OVERLAY_SOURCE)
        if results
        else 0
    )


_NOISE = text("""
    SELECT count(*),
           count(*) FILTER (WHERE f.noise_marked_at IS NOT NULL OR EXISTS (
               SELECT 1 FROM case_findings cf
               JOIN cases c ON c.case_id = cf.case_id
               JOIN case_closure_info ci ON ci.case_id = cf.case_id
               WHERE cf.finding_id = f.finding_id
                 AND c.status = 'closed'
                 AND ci.closure_category = 'false_positive'))
    FROM findings f
    WHERE f.entity_context -> 'detection_rule' @> CAST(:rule AS jsonb)
      AND f.created_at >= :since
""")


def _noise(session: Any, copy: Copy) -> Tuple[int, int]:
    """(findings naming the version since it went live, how many are noise)."""
    since = copy.stage_at.astimezone(timezone.utc).replace(tzinfo=None)
    rule = json.dumps({"rule_id": copy.rule_id, "version": copy.version})
    total, noise = session.execute(_NOISE, {"rule": rule, "since": since}).one()
    return int(total), int(noise)


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
            f"{noise} of {total} findings marked noise or closed false_positive",
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
    # A shadow copy Kibana already runs is not promoted (nor offered by PR).
    gone = siem_runs(kibana.SOURCE, [c.rule_id for c in copies if c.stage == "shadow"])
    copies = [c for c in copies if c.rule_id not in gone]
    stopped = _stopped()
    target = _deploy_target()
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
            for trigger in _deploy_runs(promoted, target):
                session.add(trigger)
                session.flush()
                report.queued.append(trigger.id)


DEPLOY_WORKFLOW = "agent-run-mitre_analyst"


def _deploy_target() -> Optional[Dict[str, Any]]:
    """The elastic deploy target, read before the stage transaction opens."""
    from core.detections.detection_rules_service import read_deploy_target

    try:
        return read_deploy_target("elastic")
    except (
        Exception
    ) as exc:  # noqa: BLE001 -- an unread target must not hold a promotion
        logger.warning("detection rules: deploy target unread: %s", exc)
        return None


def _deploy_runs(
    promoted: List[Copy], target: Optional[Dict[str, Any]]
) -> List[IntakeTrigger]:
    """A queued mitre_analyst run per promoted copy, when a target is set."""
    if not promoted or target is None:
        return []
    where = target.get("subdirectory") or "the repository root"
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
                "hypothesis": (
                    f"Open a pull request adding the live Vigil rule {copy.name!r} "
                    f"(rule_id {copy.key}, Vigil detection rule {copy.rule_id}) to "
                    f"{target['git_url']} under {where}, with the attached TOML "
                    f"(Vigil's {copy.path or 'overlay file'}) as its file content "
                    "exactly as given, and record the pull request's link on this Case."
                ),
                # The overlay file whole, exclusions included; the brief carries it.
                "document": copy.toml,
            },
        )
        for copy in promoted
    ]


def run(live: bool) -> RolloutReport:
    """Run the overlay copies within the budget, then sweep their stages.

    ``live`` False (no processor queue) skips live copies entirely, so their
    last successful run stays put and their hits are read next tick.
    """
    report = RolloutReport()
    now = datetime.now(timezone.utc)
    try:
        _run_copies(live, now, report)
    except Exception as exc:  # noqa: BLE001 -- the sync's own reports stand
        logger.warning("detection rules: overlay runs failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    try:
        # Its own step: a Revert is still due when Elasticsearch is unreachable.
        sweep(now, report)
    except Exception as exc:  # noqa: BLE001
        logger.warning("detection rules: overlay stage sweep failed: %s", exc)
        report.error = report.error or f"sweep: {type(exc).__name__}: {exc}"
    logger.info("detection rules: rollout %s", report)
    return report


def _run_copies(live: bool, now: datetime, report: RolloutReport) -> None:
    config = _elastic_config()
    if config is None:
        report.skipped = "elastic-siem not enabled or has no elasticsearch_url"
        return
    per_hour = config.get("max_searches_per_hour")
    per_hour = sees.DEFAULT_PER_HOUR if per_hour is None else max(int(per_hour), 0)
    copies = [
        c for c in _copies() if c.stage == "shadow" or (c.stage == "live" and live)
    ]
    # Worked out on each read: no stage records the handoff.
    gone = siem_runs(kibana.SOURCE, [c.rule_id for c in copies])
    report.handed_off = len(gone)
    _hand_off([c for c in copies if c.rule_id in gone], now)
    copies = [c for c in copies if c.rule_id not in gone]
    report.copies = len(copies)
    blocked = [(c, why) for c in copies if (why := runnable(c.rule))]
    ready = [c for c in copies if not runnable(c.rule)]
    results = asyncio.run(_run_all(config, ready, per_hour, now, report))
    report.written = _write_runs(ready, results, now) + _write_unrunnable(blocked, now)
    for copy in ready:
        _, docs, error = results.get(copy.rule_id, (None, [], "not run"))
        if copy.stage == "live" and not error:
            report.findings += [_finding(copy, d, now) for d in docs]
