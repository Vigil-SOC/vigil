"""What the agent detection tools read: the active rules and their latest versions.

A rule's tool is where it was read: 'repo' for a configured repo, else the SIEM
(source 'elastic'). Only active rules count as deployed; search also finds the
disabled ones, each hit saying so.

A flag is a (rule, check) whose latest recorded change went pass -> fail, on a
rule not removed. Nothing to fail, or a change to unknown, is no flag; a later
change back to pass clears it. It is a query over detection_rule_check_changes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy import Text, and_, case, cast, func, select

from core.storage.connection import get_db_manager
from core.storage.models import (
    DetectionRule,
    DetectionRuleCheckChange,
    DetectionRuleVersion,
)

ACTIVE = DetectionRule.status == "active"
LISTED = DetectionRule.status.in_(("active", "disabled"))
REPO_PREFIX = "repo:"
TOOL = case(
    (DetectionRule.source.startswith(REPO_PREFIX), "repo"), else_=DetectionRule.source
)
RUNS = DetectionRuleVersion.checks["runs"]["result"].astext
RUNS_RESULTS = ("pass", "fail", "unknown")
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
            DetectionRule.status != "removed",
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


def tool_of(source: str) -> str:
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


def _by_tool(rows: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """(tool, runs result, count) rows as rules per tool, and Runs for a SIEM's."""
    out: Dict[str, Dict[str, Any]] = {}
    for tool, result, count in rows:
        entry = out.setdefault(tool, {"rules": 0})
        entry["rules"] += count
        if tool != "repo":
            runs = entry.setdefault(
                "runs", {**dict.fromkeys(RUNS_RESULTS, 0), "unchecked": 0}
            )
            runs[result if result in RUNS_RESULTS else "unchecked"] += count
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
                select(TOOL, RUNS, func.count())
                .select_from(DetectionRule)
                .join(DetectionRuleVersion, _latest_join())
                .where(ACTIVE, declares)
                .group_by(TOOL, RUNS)
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
    return {
        "count": sum(by_source.values()),
        "by_source": by_source,
        "by_tool": by_tool,
        "detections": detections,
    }


def siem_runs_by_technique() -> Dict[str, Dict[str, int]]:
    """Each technique an active SIEM rule declares: its rules' Runs results, counted.

    A rule with no Runs result yet counts as 'unchecked'.
    """
    technique = func.unnest(DetectionRule.techniques).table_valued("t").alias("t")
    with get_db_manager().session_scope() as session:
        rows = session.execute(
            select(technique.c.t, RUNS, func.count())
            .select_from(DetectionRule)
            .join(DetectionRuleVersion, _latest_join())
            .join(technique, technique.c.t.isnot(None))
            .where(ACTIVE, ~DetectionRule.source.startswith(REPO_PREFIX))
            .group_by(technique.c.t, RUNS)
        ).all()
    out: Dict[str, Dict[str, int]] = {}
    for t, result, count in rows:
        key = result if result in RUNS_RESULTS else "unchecked"
        bucket = out.setdefault(t, {})
        bucket[key] = bucket.get(key, 0) + count
    return out


def search(
    query: str,
    language: Optional[str],
    limit: int,
    tool: Optional[str] = None,
    runs: Optional[str] = None,
    flagged: Optional[bool] = None,
) -> List[Dict]:
    """Active and disabled rules whose format, path or parsed content contains ``query``.

    ``tool`` is 'repo' or a SIEM source; ``runs`` keeps rules whose latest
    version has that Runs result; ``flagged`` keeps rules with (or without) an
    open flag. Each hit says whether it is flagged; a single hit carries its
    recent check changes.
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
    if len(hits) == 1:
        hits[0]["recent_changes"] = recent_changes(hits[0]["rule_id"])
    return hits
