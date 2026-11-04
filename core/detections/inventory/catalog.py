"""What the agent detection tools read: the active rules and their latest versions."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from sqlalchemy import Text, and_, cast, func, select

from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion

ACTIVE = DetectionRule.status == "active"


def _latest_join():
    return and_(
        DetectionRuleVersion.rule_id == DetectionRule.id,
        DetectionRuleVersion.version == DetectionRule.latest_version,
    )


def _summary(rule: DetectionRule, content: Dict[str, Any], width: int) -> Dict:
    description = content.get("description") if isinstance(content, dict) else None
    return {
        "rule_id": rule.id,
        "title": rule.name,
        "source": rule.language,
        "id": rule.native_key,
        "repo": rule.source,
        "version": rule.latest_version,
        "description": str(description or "")[:width],
        "file_path": rule.path,
    }


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
        first = session.execute(
            select(DetectionRule, DetectionRuleVersion.content)
            .join(DetectionRuleVersion, _latest_join())
            .where(ACTIVE, declares)
            .order_by(DetectionRule.id)
            .limit(top)
        ).all()
        detections = [_summary(rule, content, 200) for rule, content in first]
    return {
        "count": sum(by_source.values()),
        "by_source": by_source,
        "detections": detections,
    }


def search(query: str, language: Optional[str], limit: int) -> List[Dict]:
    """Active rules whose format, path or parsed content contains ``query``."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    haystack = (
        DetectionRule.language
        + " "
        + func.coalesce(DetectionRule.path, "")
        + " "
        + cast(DetectionRuleVersion.content, Text)
    )
    statement = (
        select(DetectionRule, DetectionRuleVersion.content)
        .join(DetectionRuleVersion, _latest_join())
        .where(ACTIVE, haystack.ilike(f"%{escaped}%", escape="\\"))
        .order_by(DetectionRule.id)
        .limit(limit)
    )
    if language:
        statement = statement.where(DetectionRule.language == language)
    with get_db_manager().session_scope() as session:
        rows = session.execute(statement).all()
        return [_summary(rule, content, 300) for rule, content in rows]
