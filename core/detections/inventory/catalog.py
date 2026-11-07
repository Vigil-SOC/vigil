"""What the agent detection tools read: the active rules and their latest versions.

A rule's tool is where it was read: 'overlay' for Vigil's own overlay source,
'repo' for any other configured repo, else the SIEM (source 'elastic' for Kibana,
'splunk' for Splunk saved searches, or 'wazuh'). Only active rules count as
deployed; search also finds the disabled ones, each hit saying so. An overlay hit
also carries its rollout stage and stage history. ``rule_for_finding`` names the
rule and version that fired a finding.

A SIEM copy may be linked to the repo rule it came from (origin_rule_id); an
overlay rule counts as a repo rule here. A repo rule's state in a SIEM is its
linked copies' (disabled, or their Runs result, each beside its Quiet), or
'not deployed' when it has none there. Every SIEM Vigil reads (Kibana,
'elastic'; Splunk, 'splunk'; the Wazuh manager, 'wazuh') is listed. Kibana,
Wazuh and Splunk rules get a Sees check; a Wazuh rule's data source is its
decoder, a Splunk search's its index/sourcetype or data model. Kibana,
Wazuh and Splunk rules get a Quiet check, an Accurate one (``accurate``) and a
Caught one (``caught``: pass, or untested); Kibana, Wazuh and Splunk rules
get Noisy and Silent (a Wazuh rule below level 3 with no alerts, or a Splunk
scheduled report, cannot alert: it passes both and is not counted as
never-firing; a Wazuh rule below level 3 that has alerts is judged on them).

A linked Kibana or Splunk copy also carries checks['drift']: fail when it was
disabled, removed or edited in the SIEM while its repo rule stayed the same.

A flag is a (rule, check) whose latest recorded change went pass -> fail, on a
rule not removed (a drift flag stays on a removed copy). Nothing to fail, or a
change to unknown, is no flag; a later change back to pass clears it. It is a
query over detection_rule_check_changes.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import Text, and_, case, cast, func, select, text, tuple_
from sqlalchemy.orm import defer

from core.detections.inventory.readers import OVERLAY_SOURCE
from core.storage.connection import get_db_manager
from core.storage.models import (
    DetectionRule,
    DetectionRuleCheckChange,
    DetectionRuleException,
    DetectionRuleStage,
    DetectionRuleVersion,
    Finding,
)

logger = logging.getLogger(__name__)

ACTIVE = DetectionRule.status == "active"
LISTED = DetectionRule.status.in_(("active", "disabled"))
REPO_PREFIX = "repo:"
OVERLAY = "overlay"
TOOL = case(
    (DetectionRule.source == OVERLAY_SOURCE, OVERLAY),
    (DetectionRule.source.startswith(REPO_PREFIX), "repo"),
    else_=DetectionRule.source,
)
RUNS = DetectionRuleVersion.checks["runs"]["result"].astext
SEES = DetectionRuleVersion.checks["sees"]["result"].astext
QUIET = DetectionRuleVersion.checks["quiet"]["result"].astext
ACCURATE = DetectionRuleVersion.checks["accurate"]["result"].astext
NOISY = DetectionRuleVersion.checks["noisy"]["result"].astext
SILENT = DetectionRuleVersion.checks["silent"]["result"].astext
CAUGHT = DetectionRuleVersion.checks["caught"]["result"].astext
# No alert across Quiet's whole 30-day search: a never-firing rule.
NEVER_FIRED = DetectionRuleVersion.checks["silent"]["days_fired"].astext == "0"
# The per-tool tally columns _by_tool reads, in order, before the count.
TALLIED = (TOOL, RUNS, SEES, QUIET, ACCURATE, NOISY, SILENT, CAUGHT, NEVER_FIRED)
RUNS_RESULTS = ("pass", "fail", "unknown")
# The rollout stages a listed copy can be in (#3995).
STAGES = ("shadow", "live", "off")
# Each rule's current stage: its latest stage row.
CURRENT_STAGE = (
    select(DetectionRuleStage.rule_id, DetectionRuleStage.stage, DetectionRuleStage.at)
    .distinct(DetectionRuleStage.rule_id)
    .order_by(
        DetectionRuleStage.rule_id,
        DetectionRuleStage.at.desc(),
        DetectionRuleStage.id.desc(),
    )
    .subquery("current_stage")
)
LINKED = DetectionRule.link_kind.in_(("same_id", "known_name", "agent"))
NOT_DEPLOYED = "not deployed"
SEES_TOOLS = ("elastic", "wazuh", "splunk")
QUIET_TOOLS = ("elastic", "wazuh", "splunk")
# The tools whose findings resolve to a rule version (rule_for_finding):
# Accurate and Caught run there.
RESOLVES = ("elastic", "wazuh", "splunk")
# Noisy, Silent and the never-firing count read a rule's own history.
HISTORY_TOOLS = ("elastic", "wazuh", "splunk")
CHANGE = DetectionRuleCheckChange
RECENT_CHANGES = 10
# A row that changed the result; a fail -> fail row only changed the reason.
RESULT_CHANGED = CHANGE.old_result.is_distinct_from(CHANGE.new_result)
# The Detections page's rule listing: health states worst first, and its caps.
HEALTH_ORDER = (
    "fails_to_run",
    "data_silent",
    "missing_field",
    "never_fired",
    "fires",
    "disabled",
    "no_fire_data",
)
# What an operator has to act on: a rule that can't fire, or never has.
ATTENTION = ("fails_to_run", "data_silent", "missing_field", "never_fired")
CANT_FIRE = ("fails_to_run", "data_silent", "missing_field")
LISTING_SIEMS = ("elastic", "splunk", "wazuh")
# The checks past Runs and Sees, as a rule's flags on the listing: a fail on
# any of them, worst first. Caught is the one a pass is worth showing.
FLAG_CHECKS = ("noisy", "quiet", "accurate", "silent", "drift")
CAUGHT_CHECK = "caught"
FLAGS = FLAG_CHECKS + (CAUGHT_CHECK,)
# A row's worst problem across every check, worst first: a rule that can't
# fire, then a failing flag, then whether it fires; caught ranks as the best.
WORST_ORDER = (
    "fails_to_run",
    "data_silent",
    "missing_field",
    *FLAG_CHECKS,
    "never_fired",
    "fires",
    CAUGHT_CHECK,
    "disabled",
    "no_fire_data",
)
# The listing's orders besides worst first (HEALTH_ORDER, then rule id). Each
# ties on rule id, so a page boundary never moves between requests.
SORTS = {
    "most_fires": lambda row: (-row["fires"], row["rule_id"]),
    "fewest_fires": lambda row: (row["fires"], row["rule_id"]),
    "name": lambda row: (row["name"].strip().casefold(), row["rule_id"]),
    "worst": lambda row: (WORST_ORDER.index(row["worst"]), row["rule_id"]),
}
FIRES_WINDOW_DAYS = 90
# Findings kept per copy for its click-through, newest first.
FIRES_LISTED = 50
RULE_ROWS = 300


def _latest_changes():
    """The latest result-changing row per (rule, check)."""
    return (
        select(CHANGE)
        .where(RESULT_CHANGED)
        .distinct(CHANGE.rule_id, CHANGE.check_key)
        .order_by(
            CHANGE.rule_id, CHANGE.check_key, CHANGE.changed_at.desc(), CHANGE.id.desc()
        )
        .subquery()
    )


def _flags():
    """(latest change, rule) for every open flag."""
    latest = _latest_changes()
    return (
        select(latest, DetectionRule)
        .join(DetectionRule, DetectionRule.id == latest.c.rule_id)
        .where(
            latest.c.old_result == "pass",
            latest.c.new_result == "fail",
        )
    )


def _change_row(c: Any) -> Dict[str, Any]:
    return {
        "check": c.check_key,
        "old": c.old_result,
        "new": c.new_result,
        "changed_at": c.changed_at.isoformat(),
        "version": c.version,
        "reason": c.reason,
    }


def _latest_join():
    return and_(
        DetectionRuleVersion.rule_id == DetectionRule.id,
        DetectionRuleVersion.version == DetectionRule.latest_version,
    )


def in_repo(source: str) -> bool:
    """A repo-side rule (a configured repo or Vigil's overlay), not a SIEM copy."""
    return source.startswith(REPO_PREFIX)


def tool_of(source: str) -> str:
    if source == OVERLAY_SOURCE:
        return OVERLAY
    return "repo" if source.startswith(REPO_PREFIX) else source


def _summary(
    rule: DetectionRule, content: Dict[str, Any], checks: Dict[str, Any], width: int
) -> Dict:
    description = content.get("description") if isinstance(content, dict) else None
    return {
        "rule_id": rule.id,
        "title": rule.name,
        "source": rule.language,
        "id": rule.native_key,
        "repo": rule.source,
        "tool": tool_of(rule.source),
        "status": rule.status,
        "version": rule.latest_version,
        "description": str(description or "")[:width],
        "file_path": rule.path,
        "checks": checks or {},
    }


def open_flags() -> List[Dict[str, Any]]:
    """Every open flag, newest first. ``change_id`` is its pass -> fail change row,
    so a check that clears and breaks again is a new flag with a new id."""
    with get_db_manager().session_scope() as session:
        flags = [
            {
                "change_id": row.id,
                "rule_id": row.rule_id,
                "name": row.DetectionRule.name,
                "tool": tool_of(row.DetectionRule.source),
                "status": row.DetectionRule.status,
                **_change_row(row),
                "since": row.changed_at.isoformat(),
            }
            for row in session.execute(_flags()).all()
        ]
    flags.sort(key=lambda f: f["since"], reverse=True)
    for flag in flags:
        del flag["changed_at"]
    return flags


def rule_health(days: int = 7) -> Dict[str, Any]:
    """The open flags, and how many flags opened and cleared in the last ``days``."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    # Cleared: a change to pass whose previous result change was pass -> fail.
    prev_old = func.lag(CHANGE.old_result).over(
        partition_by=(CHANGE.rule_id, CHANGE.check_key),
        order_by=(CHANGE.changed_at, CHANGE.id),
    )
    history = (
        select(
            CHANGE.old_result,
            CHANGE.new_result,
            CHANGE.changed_at,
            prev_old.label("prev"),
        )
        .where(RESULT_CHANGED)
        .subquery()
    )
    flags = [
        {k: v for k, v in flag.items() if k != "change_id"} for flag in open_flags()
    ]
    with get_db_manager().session_scope() as session:
        opened, cleared = session.execute(
            select(
                func.count().filter(
                    history.c.old_result == "pass", history.c.new_result == "fail"
                ),
                func.count().filter(
                    history.c.new_result == "pass",
                    history.c.old_result == "fail",
                    history.c.prev == "pass",
                ),
            ).where(history.c.changed_at >= since)
        ).one()
    return {
        "flags": flags,
        "days": days,
        "opened": opened,
        "cleared": cleared,
    }


def recent_changes(rule_id: int, limit: int = RECENT_CHANGES) -> List[Dict[str, Any]]:
    """A rule's latest recorded check changes, newest first."""
    with get_db_manager().session_scope() as session:
        rows = session.scalars(
            select(CHANGE)
            .where(CHANGE.rule_id == rule_id)
            .order_by(CHANGE.changed_at.desc(), CHANGE.id.desc())
            .limit(limit)
        ).all()
        return [_change_row(c) for c in rows]


def _tally(bucket: Dict[str, int], result: Optional[str], count: int) -> None:
    key = result if result in RUNS_RESULTS else "unchecked"
    bucket[key] = bucket.get(key, 0) + count


def _siem_tools(session: Any) -> List[str]:
    """The SIEMs with a listed rule: the tools a repo rule can be deployed to."""
    return sorted(
        session.scalars(
            select(DetectionRule.source)
            .where(~DetectionRule.source.startswith(REPO_PREFIX), LISTED)
            .distinct()
        )
    )


def _state(status: str, checks: Dict[str, Any]) -> str:
    """A SIEM copy's state: 'disabled', or its Runs result, or 'unchecked'."""
    if status == "disabled":
        return "disabled"
    result = ((checks or {}).get("runs") or {}).get("result")
    return result if result in RUNS_RESULTS else "unchecked"


def _copies(session: Any, origins: Iterable[int]) -> Dict[int, Dict[str, List[Dict]]]:
    """Each repo rule's listed, linked SIEM copies, by tool."""
    out: Dict[int, Dict[str, List[Dict]]] = {}
    ids = list(origins)
    if not ids:
        return out
    rows = session.execute(
        select(DetectionRule, DetectionRuleVersion.checks)
        .join(DetectionRuleVersion, _latest_join())
        .where(DetectionRule.origin_rule_id.in_(ids), LINKED, LISTED)
        .order_by(DetectionRule.id)
    ).all()
    for copy, checks in rows:
        out.setdefault(copy.origin_rule_id, {}).setdefault(copy.source, []).append(
            {
                "rule_id": copy.id,
                "id": copy.native_key,
                "title": copy.name,
                "state": _state(copy.status, checks),
                "runs": (checks or {}).get("runs"),
                "quiet": (checks or {}).get("quiet"),
                "accurate": (checks or {}).get("accurate"),
                "caught": (checks or {}).get("caught"),
                "link": copy.link_kind,
            }
        )
    return out


def _link(rule: DetectionRule) -> Optional[Dict[str, Any]]:
    if rule.link_kind is None:
        return None
    return {
        "origin_rule_id": rule.origin_rule_id,
        "kind": rule.link_kind,
        "by": rule.linked_by,
        "reason": rule.link_reason,
        "at": rule.linked_at.isoformat() if rule.linked_at else None,
    }


def _with_links(session: Any, hits: List[Dict], rules: List[DetectionRule]) -> None:
    """A repo hit gets ``deployed`` per SIEM; a SIEM hit its ``origin`` link."""
    repo = [r.id for r in rules if in_repo(r.source)]
    copies = _copies(session, repo)
    tools = _siem_tools(session) if repo else []
    for hit, rule in zip(hits, rules):
        if in_repo(rule.source):
            mine = copies.get(rule.id, {})
            hit["deployed"] = {t: mine.get(t) or NOT_DEPLOYED for t in tools}
        else:
            hit["origin"] = _link(rule)


def _by_tool(rows: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """(TALLIED..., count) rows as rules per tool, and for a SIEM's its Runs, Sees,
    Quiet, Accurate, Noisy, Silent and Caught, and (where Noisy and Silent run)
    its never-firing rules."""
    out: Dict[str, Dict[str, Any]] = {}
    for tool, runs, sees, quiet, accurate, noisy, silent, caught, never, count in rows:
        entry = out.setdefault(tool, {"rules": 0})
        entry["rules"] += count
        if tool not in ("repo", OVERLAY):
            for check, result in (
                ("runs", runs),
                ("sees", sees),
                ("quiet", quiet),
                ("accurate", accurate),
                ("noisy", noisy),
                ("silent", silent),
                ("caught", caught),
            ):
                bucket = entry.setdefault(
                    check, {**dict.fromkeys(RUNS_RESULTS, 0), "unchecked": 0}
                )
                _tally(bucket, result, count)
            if tool in HISTORY_TOOLS:
                entry["never_firing"] = entry.get("never_firing", 0) + (
                    count if never else 0
                )
    return out


def rule_counts() -> Dict[str, int]:
    """Active rules per format."""
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(DetectionRule.language, func.count())
            .where(ACTIVE)
            .group_by(DetectionRule.language)
        ).all()
    return {language: count for language, count in rows}


def rules_by_technique() -> Dict[str, Set[int]]:
    """Each technique an active rule declares, and the ids of those rules."""
    technique = func.unnest(DetectionRule.techniques).table_valued("t").alias("t")
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(technique.c.t, func.array_agg(DetectionRule.id))
            .select_from(DetectionRule)
            .join(technique, technique.c.t.isnot(None))
            .where(ACTIVE)
            .group_by(technique.c.t)
        ).all()
    return {t: set(ids) for t, ids in rows}


def rules_by_tactic() -> Dict[str, int]:
    """How many active rules declare each tactic (only Sigma and Elastic do)."""
    tactic = func.unnest(DetectionRule.tactics).table_valued("t").alias("t")
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(tactic.c.t, func.count())
            .select_from(DetectionRule)
            .join(tactic, tactic.c.t.isnot(None))
            .where(ACTIVE)
            .group_by(tactic.c.t)
        ).all()
    return {t: count for t, count in rows}


def techniques_per_language() -> Dict[str, int]:
    """Distinct techniques the active rules of each format declare."""
    technique = func.unnest(DetectionRule.techniques).table_valued("t").alias("t")
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(DetectionRule.language, func.count(func.distinct(technique.c.t)))
            .select_from(DetectionRule)
            .join(technique, technique.c.t.isnot(None))
            .where(ACTIVE)
            .group_by(DetectionRule.language)
        ).all()
    return {language: count for language, count in rows}


def rules_for_technique(technique: str, top: int = 5) -> Dict[str, Any]:
    """Active rules declaring ``technique``: the count by format and the first ``top``."""
    declares = DetectionRule.techniques.contains([technique])
    with get_db_manager().session_scope() as session:
        by_source = dict(
            session.execute(
                select(DetectionRule.language, func.count())
                .where(ACTIVE, declares)
                .group_by(DetectionRule.language)
            ).all()
        )
        by_tool = _by_tool(
            session.execute(
                select(*TALLIED, func.count())
                .select_from(DetectionRule)
                .join(DetectionRuleVersion, _latest_join())
                .where(ACTIVE, declares)
                .group_by(*TALLIED)
            ).all()
        )
        first = session.execute(
            select(
                DetectionRule, DetectionRuleVersion.content, DetectionRuleVersion.checks
            )
            .join(DetectionRuleVersion, _latest_join())
            .where(ACTIVE, declares)
            .order_by(DetectionRule.id)
            .limit(top)
        ).all()
        detections = [
            _summary(rule, content, checks, 200) for rule, content, checks in first
        ]
        _with_links(session, detections, [rule for rule, _, _ in first])
        deployed = _repo_deployed(session, declares)
    return {
        "count": sum(by_source.values()),
        "by_source": by_source,
        "by_tool": by_tool,
        "repo_deployed": deployed,
        "detections": detections,
    }


def _repo_deployed(session: Any, declares: Any) -> Dict[str, Dict[str, int]]:
    """Per SIEM, the active repo rules declaring a technique: their copies'
    states counted (each copy once), and the rules with no copy there."""
    tools = _siem_tools(session)
    repo_ids = session.scalars(
        select(DetectionRule.id).where(
            ACTIVE, declares, DetectionRule.source.startswith(REPO_PREFIX)
        )
    ).all()
    copies = _copies(session, repo_ids)
    out: Dict[str, Dict[str, int]] = {}
    for tool in tools:
        counts = dict.fromkeys(
            (*RUNS_RESULTS, "unchecked", "disabled", NOT_DEPLOYED), 0
        )
        for rule_id in repo_ids:
            mine = copies.get(rule_id, {}).get(tool)
            if not mine:
                counts[NOT_DEPLOYED] += 1
            for copy in mine or ():
                counts[copy["state"]] += 1
        out[tool] = counts
    return out


def siem_checks_by_technique() -> Dict[str, Dict[str, Any]]:
    """Each technique an active SIEM rule declares: its rules' Runs and Sees results
    counted, how many rules, how many fail either, and how many are proven: pass
    Runs and Sees on the same rule.

    A rule with no result yet counts as 'unchecked'.
    """
    technique = func.unnest(DetectionRule.techniques).table_valued("t").alias("t")
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(technique.c.t, RUNS, SEES, func.count())
            .select_from(DetectionRule)
            .join(DetectionRuleVersion, _latest_join())
            .join(technique, technique.c.t.isnot(None))
            .where(ACTIVE, ~DetectionRule.source.startswith(REPO_PREFIX))
            .group_by(technique.c.t, RUNS, SEES)
        ).all()
    out: Dict[str, Dict[str, Any]] = {}
    for t, runs, sees, count in rows:
        entry = out.setdefault(
            t, {"runs": {}, "sees": {}, "rules": 0, "failing": 0, "proven": 0}
        )
        _tally(entry["runs"], runs, count)
        _tally(entry["sees"], sees, count)
        entry["rules"] += count
        if "fail" in (runs, sees):
            entry["failing"] += count
        if runs == sees == "pass":
            entry["proven"] += count
    return out


def blind_spots() -> List[Dict[str, Any]]:
    """Each data source an active SIEM rule fails Sees on, and the rules it starves."""
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(DetectionRule, DetectionRuleVersion.checks["sees"])
            .join(DetectionRuleVersion, _latest_join())
            .where(ACTIVE, SEES == "fail")
            .order_by(DetectionRule.id)
        ).all()
        spots: Dict[str, List[Dict[str, Any]]] = {}
        for rule, sees in rows:
            spots.setdefault(sees.get("data_source") or "unknown", []).append(
                {
                    "rule_id": rule.id,
                    "id": rule.native_key,
                    "title": rule.name,
                    "tool": tool_of(rule.source),
                    "reason": sees.get("reason"),
                    "checked_at": sees.get("checked_at"),
                }
            )
    return [
        {"data_source": source, "rules": rules}
        for source, rules in sorted(spots.items(), key=lambda kv: -len(kv[1]))
    ]


def _any_result(result: str) -> Any:
    """A latest version with ``result`` on Runs, Sees or Quiet (drift is not
    can't-fire, nor is Accurate: a rule failing it fires, on nothing)."""
    return func.jsonb_path_exists(
        DetectionRuleVersion.checks,
        f'$ ? (@.runs.result == "{result}" || @.sees.result == "{result}"'
        f' || @.quiet.result == "{result}")',
    )


def rules_by_tool() -> Dict[str, Dict[str, Any]]:
    """Active rules per tool across the estate, with each SIEM's Runs, Sees,
    Quiet, Accurate, Noisy, Silent and Caught, and its never-firing rules."""
    with get_db_manager().session_scope() as session:
        return _by_tool(
            session.execute(
                select(*TALLIED, func.count())
                .select_from(DetectionRule)
                .join(DetectionRuleVersion, _latest_join())
                .where(ACTIVE)
                .group_by(*TALLIED)
            ).all()
        )


def unchecked(siems: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """Per check, the SIEMs it runs on and what a missing result means there.
    A SIEM not listed under a check gets no such check yet."""
    siems = sorted(siems)
    return {
        "runs": {"tools": siems, "reason": "no run recorded for this version yet"},
        "sees": {
            "tools": [t for t in siems if t in SEES_TOOLS],
            "reason": "waiting for the query budget",
        },
        "quiet": {
            "tools": [t for t in siems if t in QUIET_TOOLS],
            "reason": "waiting for the query budget",
        },
        "accurate": {
            "tools": [t for t in siems if t in RESOLVES],
            "reason": "not judged since the last sync",
        },
        "caught": {
            "tools": [t for t in siems if t in RESOLVES],
            "reason": "untested: no attack run or proven incident has caught this version",
        },
        **{
            check: {
                "tools": [t for t in siems if t in HISTORY_TOOLS],
                "reason": "waiting for the query budget",
            }
            for check in ("noisy", "silent")
        },
    }


def rules_not_passing(limit: int = 200) -> Dict[str, Any]:
    """Active SIEM rules failing or unknown on Runs, Sees or Quiet: the first
    ``limit`` with their checks as stored, broken (Runs fails) first, then starved
    (Sees fails, Runs doesn't), then the rest; how many there are; and per tool
    how many are not passing, broken and starved. Broken plus starved can't fire."""
    broken = RUNS == "fail"
    # RUNS is NULL with no run yet; a bare != would drop those rules.
    starved = and_(SEES == "fail", RUNS.is_distinct_from("fail"))
    where = (
        ACTIVE,
        ~DetectionRule.source.startswith(REPO_PREFIX),
        _any_result("fail") | _any_result("unknown"),
    )
    latest = (
        select().select_from(DetectionRule).join(DetectionRuleVersion, _latest_join())
    )
    with get_db_manager().session_scope() as session:
        by_tool = {
            tool: {"not_passing": count, "broken": b, "starved": s}
            for tool, count, b, s in session.execute(
                latest.add_columns(
                    TOOL,
                    func.count(),
                    func.count().filter(broken),
                    func.count().filter(starved),
                )
                .where(*where)
                .group_by(TOOL)
            ).all()
        }
        rows = session.execute(
            latest.add_columns(DetectionRule, DetectionRuleVersion.checks)
            .where(*where)
            .order_by(
                case((broken, 0), (SEES == "fail", 1), else_=2),
                DetectionRule.source,
                DetectionRule.name,
            )
            .limit(limit)
        ).all()
        rules = [
            {
                "rule_id": rule.id,
                "id": rule.native_key,
                "title": rule.name,
                "tool": tool_of(rule.source),
                "version": rule.latest_version,
                # The console makes a column of each key; drift is not can't-fire.
                "checks": {k: v for k, v in (checks or {}).items() if k != "drift"},
            }
            for rule, checks in rows
        ]
    return {
        "rules": rules,
        "total": sum(t["not_passing"] for t in by_tool.values()),
        "limit": limit,
        "by_tool": by_tool,
    }


def repo_coverage() -> Dict[str, int]:
    """Active repo and overlay rules, and how many have no linked copy in any SIEM."""
    with get_db_manager().session_scope() as session:
        ids = session.scalars(
            select(DetectionRule.id).where(
                ACTIVE, DetectionRule.source.startswith(REPO_PREFIX)
            )
        ).all()
        copies = _copies(session, ids)
        tools = _siem_tools(session)
    deployed = sum(1 for i in ids if copies.get(i))
    return {
        "rules": len(ids),
        "deployed": deployed,
        "not_deployed": len(ids) - deployed,
        "siems": len(tools),
    }


def _rule_of_copy(session: Any) -> Dict[int, Tuple[Any, ...]]:
    """Which rule each stored copy belongs to, so one rule's copies count once.

    A linked SIEM copy belongs to its origin. The overlay saves one rule's copies under one name.
    """
    rows = session.execute(
        select(
            DetectionRule.id,
            DetectionRule.source,
            DetectionRule.name,
            DetectionRule.origin_rule_id,
            DetectionRule.link_kind,
        )
    ).all()
    own = {
        r.id: (OVERLAY_SOURCE, r.name) if r.source == OVERLAY_SOURCE else (r.id,)
        for r in rows
    }
    return {
        r.id: (
            own[r.origin_rule_id]
            if r.origin_rule_id is not None and r.link_kind != "declined"
            else own[r.id]
        )
        for r in rows
    }


def _fires(session: Any) -> Dict[str, Dict[Any, List[Dict[str, Any]]]]:
    """Findings in the window, newest first, by the rule that fired them.

    A finding goes to the rule ``resolve_findings`` traces it to (the rule id or
    saved search the alert names, or the overlay rule that made it). One that
    doesn't resolve falls back to the rule name the alert carries, else its
    title, so a SIEM whose alerts name no rule id still counts."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=FIRES_WINDOW_DAYS
    )
    rows = session.execute(
        select(
            Finding.finding_id,
            Finding.title,
            Finding.severity,
            Finding.timestamp,
            Finding.data_source,
            Finding.entity_context,
        )
        .where(Finding.timestamp >= since)
        .order_by(Finding.timestamp.desc(), Finding.finding_id)
    ).all()
    resolved = (
        resolve_findings(
            session,
            {
                # The alert time picks the rule version: without it no SIEM
                # finding resolves.
                r.finding_id: {
                    "data_source": r.data_source,
                    "entity_context": r.entity_context,
                    "timestamp": r.timestamp,
                }
                for r in rows
            },
            full=False,
        )
        if rows
        else {}
    )
    by_rule: Dict[Any, List[Dict[str, Any]]] = {}
    by_name: Dict[Any, List[Dict[str, Any]]] = {}
    for r in rows:
        hit = {
            "finding_id": r.finding_id,
            "title": r.title,
            "severity": r.severity,
            # Stored naive, in UTC (as the window above reads it).
            "timestamp": (
                r.timestamp.replace(tzinfo=timezone.utc).isoformat()
                if r.timestamp
                else None
            ),
        }
        found = resolved.get(r.finding_id) or {}
        if found.get("resolved"):
            by_rule.setdefault(found["rule_id"], []).append(hit)
            continue
        context = r.entity_context if isinstance(r.entity_context, dict) else {}
        name = context.get("rule_name") or r.title
        if name:
            by_name.setdefault(name, []).append(hit)
    return {"by_rule": by_rule, "by_name": by_name}


def _fired(
    fires: Dict[str, Dict[Any, List[Dict[str, Any]]]], rule: Any, shared: bool
) -> Tuple[List[Dict[str, Any]], int]:
    """A copy's findings (those traced to it, then those only its name matches)
    and how many name-only findings it left out. A name more than one listed
    copy carries (``shared``) can't tell them apart, so its name-only findings
    count on none of them."""
    traced = fires.get("by_rule", {}).get(rule.id, [])
    named = fires.get("by_name", {}).get(rule.name, [])
    return (traced, len(named)) if shared else (traced + named, 0)


def _siem_alerts(checks: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """(alerts, days) the SIEM itself counted for the rule, as Quiet stored them."""
    quiet = checks.get("quiet")
    if not isinstance(quiet, dict):
        return None, None
    count, days = quiet.get("count"), quiet.get("days")
    return (
        count if isinstance(count, int) and not isinstance(count, bool) else None,
        days if isinstance(days, int) and not isinstance(days, bool) else None,
    )


def _health(
    status: str, checks: Dict[str, Any], fires: int
) -> Tuple[str, str, Optional[str]]:
    """(health, why, raw), the first that holds, worst first. ``raw`` is the
    failing check's stored reason, as the SIEM or the check wrote it."""
    runs = checks.get("runs") or {}
    sees = checks.get("sees") or {}
    silent = checks.get("silent") or {}
    if status == "disabled":
        return "disabled", "Switched off in the SIEM.", None
    if runs.get("result") == "fail":
        raw = str(runs.get("reason") or "")
        return "fails_to_run", _first_line(raw) or "Its last run failed.", raw or None
    if sees.get("result") == "fail":
        raw = str(sees.get("reason") or "")
        state = "missing_field" if "not in the mapping" in raw else "data_silent"
        return state, _first_line(raw) or "Its data is not arriving.", raw or None
    if fires:
        return "fires", f"Fired {fires} times in {FIRES_WINDOW_DAYS} days.", None
    alerts, days_counted = _siem_alerts(checks)
    if alerts:
        span = f" in {days_counted} days" if days_counted else ""
        return (
            "fires",
            f"The SIEM counted {alerts} alerts{span}; none became a finding.",
            None,
        )
    # Silent's days_fired is the SIEM's own count; with no finding and no
    # count, the fire count is unknown, not zero.
    days = silent.get("days_fired")
    if isinstance(days, int) and days > 0:
        return "fires", f"Alerted on {days} days in the SIEM.", None
    if days == 0:
        return (
            "never_fired",
            str(silent.get("reason") or "The SIEM saw no alert from it."),
            None,
        )
    return (
        "no_fire_data",
        "No finding names it, and its SIEM reports no alert counts for it.",
        None,
    )


def _check_flags(checks: Dict[str, Any]) -> Tuple[List[str], bool]:
    """The rule's failing flag checks, worst first, and whether Caught passed."""

    def result(key: str) -> Any:
        check = checks.get(key)
        return check.get("result") if isinstance(check, dict) else None

    return [k for k in FLAG_CHECKS if result(k) == "fail"], result(
        CAUGHT_CHECK
    ) == "pass"


def _worst(health: str, flags: List[str], caught: bool) -> str:
    """The row's worst problem: a rule that can't fire, or is off, stays so; else
    its worst failing flag; else Caught over Fires; else its health."""
    if health in CANT_FIRE or health == "disabled":
        return health
    if flags:
        return flags[0]
    if caught and health in ("fires", "no_fire_data", "never_fired"):
        return CAUGHT_CHECK
    return health


def _first_line(reason: str) -> str:
    """The reason's first line without its status prefix; a bare error code
    takes the next line too, so the line says what went wrong."""
    lines = [ln.strip() for ln in reason.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    line = lines[0]
    for prefix in ("fail: ", "failed: ", "skipped: ", "partial failure: "):
        if line.startswith(prefix):
            line = line[len(prefix) :]
            break
    if " " not in line and len(lines) > 1:
        line = f"{line}: {lines[-1]}"
    return line[:1].upper() + line[1:]


def _results(checks: Dict[str, Any]) -> List[Any]:
    """Each stored check's result; drift compares copies, so it is no check of the rule."""
    return [
        value.get("result")
        for key, value in checks.items()
        if key != "drift" and isinstance(value, dict)
    ]


def _by_siem(listed: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Per SIEM, its enabled copies: how many can't fire, have no Runs or Sees
    result yet, and the rest."""
    out = {
        tool: {"rules": 0, "cant_fire": 0, "not_checked": 0, "can_fire": 0}
        for tool in LISTING_SIEMS
        if any(r["tool"] == tool for r in listed)
    }
    for row in listed:
        entry = out.get(row["tool"])
        if entry is None or row["status"] != "active":
            continue
        entry["rules"] += 1
        if row["health"] in CANT_FIRE:
            entry["cant_fire"] += 1
        elif not row["runs"] and not row["sees"]:
            entry["not_checked"] += 1
        else:
            entry["can_fire"] += 1
    return out


def _haystack(row: Dict[str, Any]) -> str:
    """What a search reads, lowercased once: name, reason, techniques and where
    the rule reads its data."""
    return "\n".join(
        (row["name"], row["why"], row["where"] or "", *row["techniques"])
    ).lower()


def _where(checks: Dict[str, Any], tool: str) -> Optional[str]:
    """The data source a rule reads, as its Sees check names it: a Kibana index
    pattern, a Wazuh decoder, or a Splunk index/sourcetype or data model. A
    leading tool name ("wazuh decoder x") is dropped: the row names the tool."""
    sees = checks.get("sees")
    source = sees.get("data_source") if isinstance(sees, dict) else None
    if not isinstance(source, str) or not source.strip():
        return None
    source = source.strip()
    if source.lower().startswith(tool + " "):
        source = source[len(tool) + 1 :].strip()
    return source or None


# The listing reads every listed copy (about 2 s on 19k copies), so it is built
# once and reused for every page, filter and search until a table it reads is
# written. Postgres's own write counters say when: they move on any insert,
# update or delete, from this process or the daemon's syncs and checks.
_LISTING_TABLES = (
    "detection_rules",
    "detection_rule_versions",
    "detection_rule_check_changes",
    "detection_rule_exceptions",
    "detection_rule_stages",
)
_FIRES_TABLES = ("findings",)
_listing_lock = threading.Lock()
_listing: Dict[str, Any] = {}


def _write_marks(session: Any, tables: Tuple[str, ...]) -> Tuple[Any, ...]:
    """Each table's insert/update/delete counts. They lag a commit by at most
    Postgres's stats flush (seconds), and a stats reset only forces a rebuild."""
    rows = session.execute(
        text(
            "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables"
            " WHERE relname = ANY(:tables) ORDER BY relname"
        ),
        {"tables": list(tables)},
    ).all()
    return tuple(tuple(r) for r in rows)


def _copies_listed(session: Any) -> Dict[str, Any]:
    """Every listed copy with its checks, which rule each belongs to, and the blind spots."""
    rows = session.execute(
        select(
            DetectionRule.id,
            DetectionRule.name,
            DetectionRule.language,
            DetectionRule.source,
            DetectionRule.status,
            DetectionRule.techniques,
            DetectionRuleVersion.checks,
        )
        .join(DetectionRuleVersion, _latest_join())
        .where(LISTED)
        .order_by(DetectionRule.id)
    ).all()
    active: Dict[int, List[int]] = {}
    for rid, eid in session.execute(
        select(DetectionRuleException.rule_id, DetectionRuleException.id)
        .where(DetectionRuleException.removed_at.is_(None))
        .order_by(DetectionRuleException.id)
    ):
        active.setdefault(rid, []).append(eid)
    return {
        "rows": rows,
        "exceptions": active,
        "stages": {
            rid: (stage, at)
            for rid, stage, at in session.execute(select(CURRENT_STAGE))
        },
        "rule_of": _rule_of_copy(session),
        "blind_spots": [
            {
                "data_source": spot["data_source"],
                "tool": rule["tool"],
                "rules": sum(1 for r in spot["rules"] if r["tool"] == rule["tool"]),
            }
            for spot in blind_spots()
            for rule in {r["tool"]: r for r in spot["rules"]}.values()
        ],
    }


def _window_start(today: Optional[date] = None) -> str:
    """The first UTC day of the window the runner keeps exception matches for."""
    from core.detections.inventory.rollout import KEEP_DAYS

    today = today or datetime.now(timezone.utc).date()
    return (today - timedelta(days=KEEP_DAYS - 1)).isoformat()


def _silenced(checks: Dict[str, Any], exception_ids: List[int], since_day: str) -> int:
    """Hits these active exceptions dropped on UTC days from ``since_day`` on. A
    day the runner has not trimmed yet is outside the window and not counted."""
    counts = checks.get("exceptions") or {}
    total = 0
    for eid in exception_ids:
        mine = counts.get(str(eid))
        days = mine.get("days") if isinstance(mine, dict) else None
        if isinstance(days, dict):
            total += sum(
                v for d, v in days.items() if d >= since_day and isinstance(v, int)
            )
    return total


def _health_rows(
    copies: Dict[str, Any], fires: Dict[str, int], today: Optional[date] = None
) -> Dict[str, Any]:
    """Each copy's health row, worst first, and the totals that don't depend on a filter."""
    rule_of = copies["rule_of"]
    since_day = _window_start(today)
    excepted = copies.get("exceptions") or {}
    stages = copies.get("stages") or {}
    listed = []
    fired: Dict[int, List[Dict[str, Any]]] = {}
    # Each copy's counted findings per UTC day, so a shorter window than Fires'
    # (the exceptions panel's) counts the same findings the same way.
    fired_days: Dict[int, Dict[str, int]] = {}
    # The rules (not copies) carrying each name: a SIEM copy and its linked
    # repo origin share one, and are one rule.
    names: Dict[str, set] = {}
    for rule in copies["rows"]:
        names.setdefault(rule.name, set()).add(rule_of[rule.id])
    mapped = set()
    for rule in copies["rows"]:
        checks = rule.checks or {}
        if rule.techniques:
            mapped.add(rule_of[rule.id])
        hits, unsure = _fired(fires, rule, len(names[rule.name]) > 1)
        fired[rule.id] = hits[:FIRES_LISTED]
        days: Dict[str, int] = {}
        for hit in hits:
            if hit.get("timestamp"):
                day = hit["timestamp"][:10]
                days[day] = days.get(day, 0) + 1
        fired_days[rule.id] = days
        count = len(hits)
        alerts, days_counted = _siem_alerts(checks)
        state, why, raw = _health(rule.status, checks, count)
        flags, caught = _check_flags(checks)
        results = _results(checks)
        stage, entered = stages.get(rule.id, (None, None))
        listed.append(
            {
                "rule_id": rule.id,
                "name": rule.name,
                "language": rule.language,
                "source": rule.source,
                "tool": tool_of(rule.source),
                "status": rule.status,
                "techniques": list(rule.techniques or []),
                "where": _where(checks, tool_of(rule.source)),
                "health": state,
                "flags": flags,
                "caught": caught,
                "worst": _worst(state, flags, caught),
                "why": why,
                "raw": raw,
                "fires": count,
                # Findings that name it but also another listed copy: on neither.
                "fires_unsure": unsure,
                "siem_alerts": alerts,
                "siem_days": days_counted,
                "checks_passing": results.count("pass"),
                "checks_run": len(results),
                "runs": checks.get("runs"),
                "sees": checks.get("sees"),
                # Its active exceptions, and the hits they silenced (#3794).
                "exceptions": len(excepted.get(rule.id, [])),
                "silenced": _silenced(checks, excepted.get(rule.id, []), since_day),
                # Its overlay copy's rollout stage and when its current stage row
                # was written (#3995); days_in_shadow is worked out when read.
                "stage": stage,
                "stage_since": entered.isoformat() if entered else None,
                "days_in_shadow": None,
            }
        )
    listed.sort(key=lambda row: (HEALTH_ORDER.index(row["health"]), row["rule_id"]))
    by_state: Dict[str, List[Tuple[str, str, Dict[str, Any]]]] = {
        state: [] for state in HEALTH_ORDER
    }
    counts: Dict[Optional[str], Dict[str, int]] = {None: dict.fromkeys(HEALTH_ORDER, 0)}
    flag_counts: Dict[Optional[str], Dict[str, int]] = {None: dict.fromkeys(FLAGS, 0)}
    stage_counts: Dict[Optional[str], Dict[str, int]] = {None: dict.fromkeys(STAGES, 0)}
    for row in listed:
        if row["stage"] in STAGES:
            stage_counts[None][row["stage"]] += 1
            stage_counts.setdefault(row["tool"], dict.fromkeys(STAGES, 0))[row["stage"]] += 1
        for key in _row_flags(row):
            flag_counts[None][key] += 1
            flag_counts.setdefault(row["tool"], dict.fromkeys(FLAGS, 0))[key] += 1
        page_row = {k: v for k, v in row.items() if k not in ("runs", "sees")}
        by_state[row["health"]].append((row["tool"], _haystack(row), page_row))
        counts[None][row["health"]] += 1
        counts.setdefault(row["tool"], dict.fromkeys(HEALTH_ORDER, 0))[
            row["health"]
        ] += 1
    return {
        "total": len(listed),
        "by_state": by_state,
        "counts": counts,
        "flag_counts": flag_counts,
        "stage_counts": stage_counts,
        "never_fired_rules": len(
            {
                rule_of[row["rule_id"]]
                for row in listed
                if row["health"] == "never_fired"
            }
        ),
        "fired": fired,
        "fired_days": fired_days,
        "rule_total": len({rule_of[row["rule_id"]] for row in listed}),
        "mapped": len(mapped),
        "by_siem": _by_siem(listed),
    }


def _row_flags(row: Dict[str, Any]) -> List[str]:
    """The flag filters a row matches: its failing flags, and Caught if it passed."""
    return row["flags"] + ([CAUGHT_CHECK] if row["caught"] else [])


def _listing_now() -> Dict[str, Any]:
    """The health rows, rebuilt only when a table they read was written: the
    copies on a rules or checks write, the fire counts on a findings write or a
    new day (the window rolls)."""
    with _listing_lock, get_db_manager().session_scope() as session:
        written = _write_marks(session, _LISTING_TABLES + _FIRES_TABLES)
        marks = tuple(m for m in written if m[0] in _LISTING_TABLES)
        if _listing.get("marks") != marks:
            _listing.clear()
            _listing["copies"] = _copies_listed(session)
            _listing["marks"] = marks
        fires_marks = (
            tuple(m for m in written if m[0] in _FIRES_TABLES),
            datetime.now(timezone.utc).date(),
        )
        if _listing.get("fires_marks") != fires_marks or "health" not in _listing:
            _listing["health"] = _health_rows(_listing["copies"], _fires(session))
            _listing["fires_marks"] = fires_marks
        return {**_listing["health"], "blind_spots": _listing["copies"]["blind_spots"]}


def _with_days_in_shadow(row: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """The row with whole 24-hour days since its shadow stage began, the count
    rollout's promotion uses (``now - stage_since`` against SHADOW_DAYS days)."""
    if row.get("stage") != "shadow" or not row.get("stage_since"):
        return row
    since = now - datetime.fromisoformat(row["stage_since"])
    return {**row, "days_in_shadow": since // timedelta(days=1)}


def health_listing(
    health: Optional[str] = None,
    tool: Optional[str] = None,
    q: Optional[str] = None,
    offset: int = 0,
    sort: str = "worst",
    flag: Optional[str] = None,
    rule_ids: Optional[Iterable[int]] = None,
    has_exceptions: bool = False,
    stage: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Every listed copy's health, ``RULE_ROWS`` from ``offset``, narrowed to one
    ``health`` (or "attention", the states to act on), one ``tool``, one ``stage``
    (shadow, live or off: the copy's current rollout stage), one ``flag``
    (a failing Noisy, Quiet, Accurate, Silent or drift check, or a passing
    Caught), a ``q`` over name, technique, reason and data source, and
    ``rule_ids`` (the copies a rule-health entry names) before the cap. ``sort``
    is one of SORTS: worst first across every check (the default), most or
    fewest fires, or by name. ``has_exceptions`` lists every copy with an active
    exception, whatever its health.

    The counts cover every copy of ``tool`` (every copy when none); rule_total
    and mapped count each rule once, across every tool. With ``q``,
    ``matched_elsewhere`` counts its matches per health state outside this view.
    ``with_exceptions`` counts the copies of ``tool`` with an active exception;
    ``exceptions_elsewhere`` the ones the search keeps that this view leaves out,
    so a healthy rule's exceptions are never out of sight.
    """
    built = _listing_now()
    states = (
        HEALTH_ORDER
        if has_exceptions
        else (
            ATTENTION
            if health == "attention"
            else (health,) if health else HEALTH_ORDER
        )
    )
    needle = (q or "").strip().lower()
    ids = None if rule_ids is None else set(rule_ids)

    def kept(row_tool: str, hay: str, row: Dict[str, Any]) -> bool:
        return (
            (tool is None or row_tool == tool)
            and needle in hay
            and (ids is None or row["rule_id"] in ids)
        )

    # by_state keeps worst-first order, so the states' lists in HEALTH_ORDER are too.
    shown = [
        row
        for state in HEALTH_ORDER
        if state in states
        for row_tool, hay, row in built["by_state"][state]
        if kept(row_tool, hay, row)
        and (stage is None or row["stage"] == stage)
        and (flag is None or flag in _row_flags(row))
        and (not has_exceptions or row.get("exceptions"))
    ]
    shown_ids = {row["rule_id"] for row in shown}
    excepted = [
        (row_tool, hay, row)
        for state in HEALTH_ORDER
        for row_tool, hay, row in built["by_state"][state]
        if row.get("exceptions")
    ]
    # A search that misses here may hit in a view the chips leave out: say where,
    # so a healthy rule's name never reads as "no such rule".
    elsewhere = {
        state: n
        for state in HEALTH_ORDER
        if needle and state not in states
        if (
            n := sum(
                1
                for row_tool, hay, row in built["by_state"][state]
                if kept(row_tool, hay, row)
            )
        )
    }
    shown = sorted(shown, key=SORTS[sort])
    return {
        "total": built["total"],
        "rule_total": built["rule_total"],
        "mapped": built["mapped"],
        "counts": built["counts"].get(tool, dict.fromkeys(HEALTH_ORDER, 0)),
        "flag_counts": built["flag_counts"].get(tool, dict.fromkeys(FLAGS, 0)),
        "stage_counts": built.get("stage_counts", {}).get(tool, dict.fromkeys(STAGES, 0)),
        "never_fired_rules": built.get("never_fired_rules", 0),
        "matched": len(shown),
        "matched_elsewhere": elsewhere,
        "with_exceptions": sum(
            1 for t, _h, _r in excepted if tool is None or t == tool
        ),
        "exceptions_elsewhere": sum(
            1
            for t, hay, row in excepted
            if kept(t, hay, row) and row["rule_id"] not in shown_ids
        ),
        "offset": offset,
        "rules": [
            _with_days_in_shadow(row, now or datetime.now(timezone.utc))
            for row in shown[offset : offset + RULE_ROWS]
        ],
        "by_siem": built["by_siem"],
        "blind_spots": built["blind_spots"],
        "fires_window_days": FIRES_WINDOW_DAYS,
        "limits": (
            "Fires counts the findings Vigil traced to the rule (by the rule id or "
            "saved search its alert names), else the ones that name it, in the last "
            f"{FIRES_WINDOW_DAYS} days. A rule with none whose SIEM counted alerts "
            "(Quiet) still fires; with neither, and a Silent check that saw no alert, it "
            "never fired; with no count at all it reads No fire data. Never fired "
            "counts rules once, however many copies. Runs and Sees results exist only "
            "for rules Vigil reads from Kibana, Wazuh and Splunk."
        ),
    }


def fired_in_window(rule_id: int, since_day: str) -> int:
    """A listed copy's Fires count from UTC day ``since_day`` on: the findings
    traced to it (or, when no other listed copy shares its name, naming it), as
    the Fires column counts them."""
    days = _listing_now().get("fired_days", {}).get(rule_id, {})
    return sum(n for day, n in days.items() if day >= since_day)


def rule_findings(rule_id: int) -> Dict[str, Any]:
    """The findings a listed copy fired in the window, newest first
    (``FIRES_LISTED`` at most), and how many in all."""
    built = _listing_now()
    for state in HEALTH_ORDER:
        for _tool, _hay, row in built["by_state"][state]:
            if row["rule_id"] == rule_id:
                return {
                    "rule_id": rule_id,
                    "name": row["name"],
                    "total": row["fires"],
                    "days": FIRES_WINDOW_DAYS,
                    "findings": built["fired"].get(rule_id, []),
                    "unsure": row.get("fires_unsure", 0),
                }
    return {"rule_id": rule_id, "total": 0, "days": FIRES_WINDOW_DAYS, "findings": []}


def search(
    query: str,
    language: Optional[str],
    limit: int,
    tool: Optional[str] = None,
    runs: Optional[str] = None,
    stage: Optional[str] = None,
    sees: Optional[str] = None,
    flagged: Optional[bool] = None,
    quiet: Optional[str] = None,
    accurate: Optional[str] = None,
    caught: Optional[str] = None,
) -> List[Dict]:
    """Active and disabled rules whose format, path or parsed content contains ``query``.

    ``tool`` is 'overlay', 'repo' or a SIEM source; ``runs``, ``sees`` and
    ``quiet`` and ``accurate`` keep rules whose latest version has that check result;
    ``caught`` 'pass' keeps caught versions, 'untested' the rest; ``stage`` keeps
    rules currently there; ``flagged`` keeps rules with (or without) an open
    flag. Each hit says whether it is flagged; a single hit carries its recent
    check changes.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    haystack = (
        DetectionRule.language
        + " "
        + func.coalesce(DetectionRule.path, "")
        + " "
        + cast(DetectionRuleVersion.content, Text)
    )
    statement = (
        select(DetectionRule, DetectionRuleVersion.content, DetectionRuleVersion.checks)
        .join(DetectionRuleVersion, _latest_join())
        .where(LISTED, haystack.ilike(f"%{escaped}%", escape="\\"))
        .order_by(DetectionRule.id)
        .limit(limit)
    )
    if language:
        statement = statement.where(DetectionRule.language == language)
    if tool:
        statement = statement.where(TOOL == tool)
    if runs:
        statement = statement.where(RUNS == runs)
    if stage:
        statement = statement.join(
            CURRENT_STAGE, CURRENT_STAGE.c.rule_id == DetectionRule.id
        ).where(CURRENT_STAGE.c.stage == stage)
    if sees:
        statement = statement.where(SEES == sees)
    if quiet:
        statement = statement.where(QUIET == quiet)
    if accurate:
        statement = statement.where(ACCURATE == accurate)
    if caught:
        statement = statement.where(
            CAUGHT.is_(None) if caught == "untested" else CAUGHT == caught
        )
    flags = _flags().subquery()
    if flagged is not None:
        is_flagged = DetectionRule.id.in_(select(flags.c.rule_id))
        statement = statement.where(is_flagged if flagged else ~is_flagged)
    with get_db_manager().session_scope() as session:
        rows = session.execute(statement).all()
        ids = [rule.id for rule, _, _ in rows]
        flagged_ids = set(
            session.scalars(
                select(flags.c.rule_id).where(flags.c.rule_id.in_(ids))
            ).all()
        )
        hits = [
            {
                **_summary(rule, content, checks, 300),
                "flagged": rule.id in flagged_ids,
            }
            for rule, content, checks in rows
        ]
        _with_links(session, hits, [rule for rule, _, _ in rows])
        overlay = [hit for hit in hits if hit["tool"] == OVERLAY]
        if overlay:
            history = _stage_rows(session, [hit["rule_id"] for hit in overlay])
            for hit in overlay:
                hit["stages"] = history.get(hit["rule_id"], [])
                hit["stage"] = hit["stages"][-1]["stage"] if hit["stages"] else None
    if len(hits) == 1:
        hits[0]["recent_changes"] = recent_changes(hits[0]["rule_id"])
    return hits


def _stage_rows(session: Any, rule_ids: List[int]) -> Dict[int, List[Dict]]:
    """Every stage row of ``rule_ids``, oldest first, by rule."""
    out: Dict[int, List[Dict]] = {}
    for row in session.scalars(
        select(DetectionRuleStage)
        .where(DetectionRuleStage.rule_id.in_(rule_ids))
        .order_by(DetectionRuleStage.rule_id, DetectionRuleStage.id)
    ):
        out.setdefault(row.rule_id, []).append(
            {
                "version": row.version,
                "stage": row.stage,
                "proof": row.proof,
                "actor": row.actor,
                "reason": row.reason,
                "at": row.at.isoformat(),
            }
        )
    return out


def _dig(value: Any, *keys: str) -> Any:
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _kibana_rule_id(record: Any) -> Optional[str]:
    """The Kibana rule_id an alert document names, flat-dotted or nested."""
    source = _dig(record, "_source") or record
    for value in (
        _dig(source, "kibana.alert.rule.rule_id"),
        _dig(source, "kibana", "alert", "rule", "rule_id"),
        _dig(source, "signal", "rule", "rule_id"),
    ):
        if value and isinstance(value, (str, int)):
            return str(value)
    return None


def _when(value: Any) -> Optional[datetime]:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _unknown(reason: str) -> Dict[str, Any]:
    return {"resolved": False, "reason": reason}


def _wazuh_rule_id(record: Any) -> Optional[str]:
    """The Wazuh rule id an alert document names, nested or flat-dotted.

    A Wazuh alert's rule always has a level; an ECS rule.id alone is not Wazuh's.
    """
    source = _dig(record, "_source") or record
    for value, level in (
        (_dig(source, "rule", "id"), _dig(source, "rule", "level")),
        (_dig(source, "rule.id"), _dig(source, "rule.level")),
    ):
        if (
            value not in (None, "")
            and isinstance(value, (str, int))
            and level is not None
        ):
            return str(value)
    return None


def _text(record: Any, field: str) -> Optional[str]:
    value = record.get(field) if isinstance(record, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


# A scheduled search's sid: scheduler_<owner>_<app>_<search>_at_<epoch>_<n>
# ('rt_scheduler_' when real-time), owner and app each written '_<name>' when
# plain alphanumeric, else as unpadded base64 (nobody_U3BsdW5rX1NBX0NJTQ is
# nobody, Splunk_SA_CIM); the search is '_<name>' or RMD5<hash>.
_SID = re.compile(r"(?:rt_)?scheduler_(.+?)_at_\d")


def _sid_owner_app(sid: Optional[str]) -> Optional[Tuple[str, str]]:
    """(owner, app) a scheduler sid names, or None when it is not readable."""
    match = _SID.match(sid or "")
    if not match:
        return None
    tokens = iter(match.group(1).split("_"))
    parts: List[str] = []
    for token in tokens:
        if len(parts) == 2:
            break  # the search part, which the notable's search_name gives
        if token == "":
            parts.append(next(tokens, ""))
            continue
        try:
            parts.append(
                base64.b64decode(
                    token + "=" * (-len(token) % 4), validate=True
                ).decode()
            )
        except (binascii.Error, UnicodeDecodeError):
            return None
    else:
        return None  # no search part after owner and app: not a scheduler sid
    return (parts[0], parts[1]) if all(parts) else None


# One key=value pair of a Splunk stash record (an ES notable's _raw), its value
# quoted with backslash escapes or bare to a comma or space; a key only at a
# field boundary. A lone quoted string is matched too, so nothing inside one
# reads as a pair.
_STASH = re.compile(
    r'(?:(?<=[,\s])|^)([A-Za-z_][\w.]*)=("(?:\\.|[^"\\])*"|[^,\s"]*)'
    r'|"(?:\\.|[^"\\])*"'
)


def _stash_fields(raw: Any) -> Dict[str, str]:
    """search_name and orig_sid a stash _raw names, first match of each."""
    fields: Dict[str, str] = {}
    for match in _STASH.finditer(raw if isinstance(raw, str) else ""):
        key, value = match.groups()
        if key in ("search_name", "orig_sid") and key not in fields:
            if value.startswith('"'):
                value = re.sub(r"\\(.)", r"\1", value[1:-1])
            fields[key] = value
    return fields


def _splunk_ref(records: List[Any]) -> Any:
    """(saved search name, inventory keys to try in order) a notable names, or why not.

    A record's top-level search_name and orig_sid are read first; without them,
    the pair its _raw stash record names (a notable fetched before the adapter
    asked for every field kept only Splunk's default fields, _raw among them).
    The keys are saved_search_key's: app/name@owner (shared with its owner only),
    then app/name. With no readable orig_sid there are none: match on name alone.
    """
    for record in records:
        if _text(record, "search_name"):
            name, sid = _text(record, "search_name"), _text(record, "orig_sid")
            break
        stash = _stash_fields(record.get("_raw") if isinstance(record, dict) else None)
        if _text(stash, "search_name"):
            name, sid = _text(stash, "search_name"), _text(stash, "orig_sid")
            break
    else:
        return _unknown("the Splunk alert names no saved search (no search_name)")
    owner_app = _sid_owner_app(sid)
    if owner_app is None:
        return name, ()
    owner, app = owner_app
    return name, (f"{app}/{name}@{owner}", f"{app}/{name}")


# Inventory source -> the SIEM a reason names.
SIEM_NAMES = {"elastic": "Kibana", "wazuh": "Wazuh", "splunk": "Splunk"}
# The key readers, in order: a Kibana rule wins over a Wazuh one.
KEY_READERS = (("elastic", _kibana_rule_id), ("wazuh", _wazuh_rule_id))


def _named(finding: Mapping[str, Any]) -> Optional[Tuple[int, int]]:
    """(rule id, version) an overlay runner's finding names, or None."""
    context = finding.get("entity_context")
    named = context.get("detection_rule") if isinstance(context, dict) else None
    if (
        not isinstance(named, dict)
        or not named.get("rule_id")
        or not named.get("version")
    ):
        return None
    try:
        return int(named["rule_id"]), int(named["version"])
    except (TypeError, ValueError):
        return None


def _rule_ref(finding: Mapping[str, Any]) -> Any:
    """(inventory source, native keys to try, alert time, Splunk search name) the
    finding names, or why it is unknown. No keys: match the search name alone."""
    source = finding.get("data_source")
    if source not in ("elastic", "splunk"):
        return _unknown(
            f"only Kibana, Wazuh and Splunk rules resolve; this finding is from {source}"
        )
    context = finding.get("entity_context")
    evidence = context.get("source_evidence") if isinstance(context, dict) else None
    records = evidence.get("records") if isinstance(evidence, dict) else None
    records = records if isinstance(records, list) else []
    name = None
    if source == "splunk":
        ref = _splunk_ref(records)
        if not isinstance(ref, tuple):
            return ref
        siem, (name, keys) = "splunk", ref
    else:
        for siem, read in KEY_READERS:
            key = next(filter(None, map(read, records)), None)
            if key is not None:
                keys = (key,)
                break
        else:
            return _unknown("the alert names no Kibana or Wazuh detection rule")
    fired = _when(finding.get("timestamp"))
    if fired is None:
        return _unknown("the finding has no alert time to pick a version by")
    return siem, keys, fired, name


def rule_for_finding(finding: Mapping[str, Any]) -> Dict[str, Any]:
    """The inventory rule and version that fired a finding, or why it is unknown.

    A finding the overlay runner made names its rule and version
    (entity_context.detection_rule) and resolves to exactly that. Otherwise it is
    read from the stored alert (entity_context.source_evidence); the version is
    the latest one first seen at or before the alert. Kibana (source 'elastic')
    and Wazuh (source 'wazuh') rules resolve by the rule id the alert names. A
    Splunk notable resolves by its search_name, with app and owner read from its
    orig_sid (its top-level fields, else its _raw stash record); with no
    readable sid, by search_name alone only when exactly one Splunk saved search
    has that name.
    """
    if _named(finding) is None:
        ref = _rule_ref(finding)
        if not isinstance(ref, tuple):
            return ref  # unknown without reading the inventory
    try:
        with get_db_manager().session_scope() as session:
            return resolve_findings(session, {0: finding})[0]
    except Exception:  # a finding read must not fail on the inventory
        logger.exception("Could not resolve the rule of a finding")
        return _unknown("the rule inventory could not be read")


def resolve_findings(
    session: Any, findings: Mapping[Any, Mapping[str, Any]], full: bool = True
) -> Dict[Any, Dict[str, Any]]:
    """``rule_for_finding`` for many findings in one session: the one mapping.

    ``full`` False leaves out the version's query and checks.
    """
    named = {k: n for k, f in findings.items() if (n := _named(f)) is not None}
    refs = {k: _rule_ref(f) for k, f in findings.items() if k not in named}
    wanted = {
        (r[0], key) for r in refs.values() if isinstance(r, tuple) for key in r[1]
    }
    # Splunk notables with no readable sid: matched on the saved search's name.
    names = {r[3] for r in refs.values() if isinstance(r, tuple) and not r[1]}
    rules: Dict[tuple, DetectionRule] = {}
    by_name: Dict[str, List[DetectionRule]] = {}
    versions: Dict[int, List[DetectionRuleVersion]] = {}
    if wanted:
        rules = {
            (r.source, r.native_key): r
            for r in session.scalars(
                select(DetectionRule).where(
                    tuple_(DetectionRule.source, DetectionRule.native_key).in_(wanted)
                )
            )
        }
    if names:
        for r in session.scalars(
            select(DetectionRule).where(
                DetectionRule.source == "splunk", DetectionRule.name.in_(names)
            )
        ):
            by_name.setdefault(r.name, []).append(r)
    ids = {r.id for r in rules.values()} | {
        r.id for found in by_name.values() for r in found
    }
    if ids:
        loads = [defer(DetectionRuleVersion.content)]
        if not full:
            loads += [
                defer(DetectionRuleVersion.query),
                defer(DetectionRuleVersion.checks),
            ]
        for v in session.scalars(
            select(DetectionRuleVersion)
            .options(*loads)
            .where(DetectionRuleVersion.rule_id.in_(ids))
            .order_by(DetectionRuleVersion.rule_id, DetectionRuleVersion.version.desc())
        ):
            versions.setdefault(v.rule_id, []).append(v)
    out: Dict[Any, Dict[str, Any]] = {}
    for k, (rule_id, number) in named.items():
        rule = session.get(DetectionRule, rule_id)
        row = session.get(DetectionRuleVersion, (rule_id, number)) if rule else None
        out[k] = (
            _unknown(f"rule {rule_id} v{number} is not in the inventory")
            if row is None
            else _resolved(rule, row, full)
        )
    for k, ref in refs.items():
        if not isinstance(ref, tuple):
            out[k] = ref
            continue
        siem, keys, fired, name = ref
        if keys:
            rule = next((rules[(siem, n)] for n in keys if (siem, n) in rules), None)
            missing = f"{SIEM_NAMES[siem]} rule {keys[-1]} is not in the inventory"
        else:
            found = by_name.get(name, [])
            rule = found[0] if len(found) == 1 else None
            missing = (
                f"Splunk saved search name {name!r} is not unique "
                f"({', '.join(sorted(r.native_key for r in found))}) and the "
                "notable has no readable orig_sid to tell them apart"
                if found
                else f"no Splunk saved search named {name!r} is in the inventory"
            )
        out[k] = (
            _unknown(missing)
            if rule is None
            else _pick(rule, versions.get(rule.id, []), fired, full)
        )
    return out


def _pick(
    rule: DetectionRule,
    versions: List[DetectionRuleVersion],
    fired: datetime,
    full: bool,
) -> Dict[str, Any]:
    """The latest of ``versions`` (newest first) first seen at or before ``fired``."""
    version = next((v for v in versions if v.first_seen <= fired), None)
    if version is None:
        first = min((v.first_seen for v in versions), default=None)
        return _unknown(
            f"the alert predates the first sync of {SIEM_NAMES[rule.source]} rule "
            f"{rule.native_key}"
            + (
                f" ({first.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC)"
                if first
                else ""
            )
        )
    return _resolved(rule, version, full)


def _resolved(
    rule: DetectionRule, version: DetectionRuleVersion, full: bool = True
) -> Dict[str, Any]:
    out = {
        "resolved": True,
        "rule_id": rule.id,
        "source": rule.source,
        "native_key": rule.native_key,
        "name": rule.name,
        "status": rule.status,
        "version": version.version,
        "latest_version": rule.latest_version,
        "first_seen": version.first_seen.isoformat(),
    }
    if full:
        out.update(query=version.query, checks=version.checks or {})
    return out
