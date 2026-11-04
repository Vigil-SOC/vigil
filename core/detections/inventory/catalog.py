"""What the agent detection tools read: the active rules and their latest versions.

A rule's tool is where it was read: 'overlay' for Vigil's own overlay source,
'repo' for any other configured repo, else the SIEM (source 'elastic'). Only
active rules count as deployed; search also finds the disabled ones, each hit
saying so. An overlay hit also carries its rollout stage and stage history.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from sqlalchemy import Text, and_, case, cast, func, select

from core.detections.inventory.readers import OVERLAY_SOURCE
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleStage, DetectionRuleVersion

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
RUNS_RESULTS = ("pass", "fail", "unknown")
# Each rule's current stage: its latest stage row.
CURRENT_STAGE = (
    select(DetectionRuleStage.rule_id, DetectionRuleStage.stage)
    .distinct(DetectionRuleStage.rule_id)
    .order_by(DetectionRuleStage.rule_id, DetectionRuleStage.id.desc())
    .subquery("current_stage")
)


def _latest_join():
    return and_(
        DetectionRuleVersion.rule_id == DetectionRule.id,
        DetectionRuleVersion.version == DetectionRule.latest_version,
    )


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


def _by_tool(rows: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """(tool, runs result, count) rows as rules per tool, and Runs for a SIEM's."""
    out: Dict[str, Dict[str, Any]] = {}
    for tool, result, count in rows:
        entry = out.setdefault(tool, {"rules": 0})
        entry["rules"] += count
        if tool not in ("repo", OVERLAY):
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
    stage: Optional[str] = None,
) -> List[Dict]:
    """Active and disabled rules whose format, path or parsed content contains ``query``.

    ``tool`` is 'overlay', 'repo' or a SIEM source; ``runs`` keeps rules whose
    latest version has that Runs result; ``stage`` keeps rules currently there.
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
    with get_db_manager().session_scope() as session:
        rows = session.execute(statement).all()
        hits = [_summary(rule, content, checks, 300) for rule, content, checks in rows]
        overlay = [hit for hit in hits if hit["tool"] == OVERLAY]
        if overlay:
            history = _stage_rows(session, [hit["rule_id"] for hit in overlay])
            for hit in overlay:
                hit["stages"] = history.get(hit["rule_id"], [])
                hit["stage"] = hit["stages"][-1]["stage"] if hit["stages"] else None
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
