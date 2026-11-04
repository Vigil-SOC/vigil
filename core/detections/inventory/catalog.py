"""What the agent detection tools read: the active rules and their latest versions.

A rule's tool is where it was read: 'overlay' for Vigil's own overlay source,
'repo' for any other configured repo, else the SIEM (source 'elastic'). Only
active rules count as deployed; search also finds the disabled ones, each hit
saying so. An overlay hit also carries its rollout stage and stage history.

A SIEM copy may be linked to the repo rule it came from (origin_rule_id); an
overlay rule counts as a repo rule here. A repo rule's state in a SIEM is its
linked copies' (disabled, or their Runs result), or 'not deployed' when it has
none there. Only SIEMs Vigil reads (today Kibana, 'elastic') are listed: Splunk
saved searches and Wazuh rules are not read yet.
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
SEES = DetectionRuleVersion.checks["sees"]["result"].astext
RUNS_RESULTS = ("pass", "fail", "unknown")
# Each rule's current stage: its latest stage row.
CURRENT_STAGE = (
    select(DetectionRuleStage.rule_id, DetectionRuleStage.stage)
    .distinct(DetectionRuleStage.rule_id)
    .order_by(DetectionRuleStage.rule_id, DetectionRuleStage.id.desc())
    .subquery("current_stage")
)
LINKED = DetectionRule.link_kind.in_(("same_id", "agent"))
NOT_DEPLOYED = "not deployed"
NOT_READ = "Splunk saved searches and Wazuh rules are not read yet"


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
    """(tool, runs, sees, count) rows as rules per tool, and Runs and Sees for a SIEM's."""
    out: Dict[str, Dict[str, Any]] = {}
    for tool, runs, sees, count in rows:
        entry = out.setdefault(tool, {"rules": 0})
        entry["rules"] += count
        if tool not in ("repo", OVERLAY):
            for check, result in (("runs", runs), ("sees", sees)):
                bucket = entry.setdefault(
                    check, {**dict.fromkeys(RUNS_RESULTS, 0), "unchecked": 0}
                )
                _tally(bucket, result, count)
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
                select(TOOL, RUNS, SEES, func.count())
                .select_from(DetectionRule)
                .join(DetectionRuleVersion, _latest_join())
                .where(ACTIVE, declares)
                .group_by(TOOL, RUNS, SEES)
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


def search(
    query: str,
    language: Optional[str],
    limit: int,
    tool: Optional[str] = None,
    runs: Optional[str] = None,
    stage: Optional[str] = None,
    sees: Optional[str] = None,
) -> List[Dict]:
    """Active and disabled rules whose format, path or parsed content contains ``query``.

    ``tool`` is 'overlay', 'repo' or a SIEM source; ``runs`` and ``sees`` keep
    rules whose latest version has that Runs or Sees result; ``stage`` keeps
    rules currently there.
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
    with get_db_manager().session_scope() as session:
        rows = session.execute(statement).all()
        hits = [_summary(rule, content, checks, 300) for rule, content, checks in rows]
        _with_links(session, hits, [rule for rule, _, _ in rows])
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
