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
Wazuh and Splunk rules get a Quiet check and an Accurate one (``accurate``);
Kibana and Splunk rules Noisy and Silent (a Splunk scheduled report, which
can't alert, passes both and is not counted as never-firing).

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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import Text, and_, case, cast, func, select, tuple_
from sqlalchemy.orm import defer

from core.detections.inventory.readers import OVERLAY_SOURCE
from core.storage.connection import get_db_manager
from core.storage.models import (
    DetectionRule,
    DetectionRuleCheckChange,
    DetectionRuleStage,
    DetectionRuleVersion,
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
# No alert across Quiet's whole 30-day search: a never-firing rule.
NEVER_FIRED = DetectionRuleVersion.checks["silent"]["days_fired"].astext == "0"
# The per-tool tally columns _by_tool reads, in order, before the count.
TALLIED = (TOOL, RUNS, SEES, QUIET, ACCURATE, NOISY, SILENT, NEVER_FIRED)
RUNS_RESULTS = ("pass", "fail", "unknown")
# Each rule's current stage: its latest stage row.
CURRENT_STAGE = (
    select(DetectionRuleStage.rule_id, DetectionRuleStage.stage)
    .distinct(DetectionRuleStage.rule_id)
    .order_by(DetectionRuleStage.rule_id, DetectionRuleStage.id.desc())
    .subquery("current_stage")
)
LINKED = DetectionRule.link_kind.in_(("same_id", "known_name", "agent"))
NOT_DEPLOYED = "not deployed"
NOT_READ = (
    "Wazuh rules get no Noisy or Silent check (nor a never-firing count), yet"
)
SEES_TOOLS = ("elastic", "wazuh", "splunk")
QUIET_TOOLS = ("elastic", "wazuh", "splunk")
# The tools whose findings resolve to a rule (rule_for_finding) and get Accurate.
ACCURATE_TOOLS = ("elastic", "wazuh", "splunk")
# Noisy, Silent and the never-firing count read a rule's own history.
HISTORY_TOOLS = ("elastic", "splunk")
CHANGE = DetectionRuleCheckChange
RECENT_CHANGES = 10
# A row that changed the result; a fail -> fail row only changed the reason.
RESULT_CHANGED = CHANGE.old_result.is_distinct_from(CHANGE.new_result)


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
    with get_db_manager().session_scope() as session:
        flags = [
            {
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
    for flag in flags:
        del flag["changed_at"]
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
    Quiet, Accurate, Noisy and Silent, and (where Noisy and Silent run) its
    never-firing rules."""
    out: Dict[str, Dict[str, Any]] = {}
    for tool, runs, sees, quiet, accurate, noisy, silent, never, count in rows:
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
        "not_read": NOT_READ,
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
    counted, how many rules, and how many fail either.

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
        entry = out.setdefault(t, {"runs": {}, "sees": {}, "rules": 0, "failing": 0})
        _tally(entry["runs"], runs, count)
        _tally(entry["sees"], sees, count)
        entry["rules"] += count
        if "fail" in (runs, sees):
            entry["failing"] += count
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
    Quiet, Accurate, Noisy and Silent, and its never-firing rules."""
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
            "tools": [t for t in siems if t in ACCURATE_TOOLS],
            "reason": "not judged since the last sync",
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
    """Active SIEM rules failing or unknown on Runs, Sees or Quiet, fails first: the first
    ``limit`` with their checks as stored, how many there are, and per tool how
    many are not passing and how many fail (can't fire)."""
    fails = _any_result("fail")
    where = (
        ACTIVE,
        ~DetectionRule.source.startswith(REPO_PREFIX),
        fails | _any_result("unknown"),
    )
    latest = (
        select().select_from(DetectionRule).join(DetectionRuleVersion, _latest_join())
    )
    with get_db_manager().session_scope() as session:
        by_tool = {
            tool: {"not_passing": count, "failing": failing}
            for tool, count, failing in session.execute(
                latest.add_columns(TOOL, func.count(), func.count().filter(fails))
                .where(*where)
                .group_by(TOOL)
            ).all()
        }
        rows = session.execute(
            latest.add_columns(DetectionRule, DetectionRuleVersion.checks)
            .where(*where)
            .order_by(fails.desc(), DetectionRule.source, DetectionRule.name)
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
) -> List[Dict]:
    """Active and disabled rules whose format, path or parsed content contains ``query``.

    ``tool`` is 'overlay', 'repo' or a SIEM source; ``runs``, ``sees`` and
    ``quiet`` and ``accurate`` keep rules whose latest version has that check result; ``stage`` keeps
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


def _splunk_ref(records: List[Any]) -> Any:
    """(saved search name, inventory keys to try in order) a notable names, or why not.

    The keys are saved_search_key's: app/name@owner (shared with its owner only),
    then app/name. With no readable orig_sid there are none: match on name alone.
    """
    notable = next((r for r in records if _text(r, "search_name")), None)
    if notable is None:
        return _unknown("the Splunk alert names no saved search (no search_name)")
    name = _text(notable, "search_name")
    owner_app = _sid_owner_app(_text(notable, "orig_sid"))
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
    orig_sid; with no readable sid, by search_name alone only when exactly one
    Splunk saved search has that name.
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
