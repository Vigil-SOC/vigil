"""Accurate: the share of each Kibana, Wazuh and Splunk rule's alerts judged real.

Each elastic or splunk finding is resolved to its rule version with
``rule_for_finding``'s own mapping (``catalog.resolve_findings``). Only findings
of the latest version of an active rule of a ``catalog.RESOLVES`` source
count, with no time cap.

A finding's determinations are its noise mark (not real) and each closed Case
it is in whose closure category CLOSURE_OUTCOMES maps to proven (real) or
false_positive (not real); an open or reopened Case, or an inconclusive or
duplicate close, determines nothing. The latest analyst determination wins,
else the latest agent one. A noise mark is the analyst's when its kind says so;
a close is when ``closed_by_person`` says so.

Fewer than MIN_JUDGED judged reads unknown; under FAIL_BELOW real reads fail;
else pass. A Wazuh rule whose level is below elastic-siem's min_rule_level, when
it reads the indexer directly (no kibana_url), is never ingested: its unknown
names that floor instead of "not enough data" (``logic.level``, read from the
version's content). The result goes under checks['accurate'] on the latest
version, written only when result, reason or counts changed; a result or reason
change is also appended to detection_rule_check_changes. Beside the four totals,
the same payload counts the judged-false split: expected, too_broad, data, other
and not_given (those add up to judged minus real), false_person and false_agent,
and case_close (a winning false-positive close, once per finding). Local SQL
only: no SIEM search.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from sqlalchemy import and_, bindparam, select, tuple_, update

from core.cases.closure import (
    ClosedByKind,
    FalsePositiveCause,
    closed_by_person,
    closure_says_real,
)
from core.detections.inventory import catalog
from core.detections.inventory.readers import OVERLAY_SOURCE
from core.storage.connection import get_db_manager
from core.storage.models import (
    Case,
    CaseClosureInfo,
    DetectionRule,
    DetectionRuleVersion,
    Finding,
    case_findings,
)

logger = logging.getLogger(__name__)

MIN_JUDGED = 20
FAIL_BELOW = 0.10
# The finding data_sources that resolve: "elastic" carries Kibana and Wazuh
# alerts, "splunk" Splunk notables.
FINDING_SOURCES = ("elastic", "splunk")
# Judged-false causes. not_given is a count, not a stored cause. The five add
# up to judged minus real, as do false_person and false_agent.
FALSE_CAUSES = ("expected", "too_broad", "data", "other", "not_given")
FALSE_SPLIT = FALSE_CAUSES + ("false_person", "false_agent", "case_close")
COUNTS = ("real", "judged", "analyst", "agent") + FALSE_SPLIT
_CAUSES = frozenset(cause.value for cause in FalsePositiveCause)
# A page of one rule's judged-false determinations, same cap as search_findings.
FALSE_PAGE = 25


class Determination(NamedTuple):
    """One determination of a finding. ``case_close`` is a false-positive close."""

    at: datetime
    analyst: bool
    real: bool
    cause: Optional[str] = None
    case_close: bool = False
    text: str = ""


@dataclass
class AccurateReport:
    source: str = "accurate"
    rules: int = 0
    findings: int = 0
    judged: int = 0
    written: int = 0
    error: Optional[str] = None


def _utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _known_cause(value: Optional[str]) -> Optional[str]:
    return value if value in _CAUSES else None


def _free_text(*values: Optional[str]) -> str:
    """The first text that was set. Blank is not set."""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _winning(found: List[Determination]) -> Optional[Determination]:
    """The latest analyst determination, else the latest agent one."""
    if not found:
        return None
    analyst = [d for d in found if d.analyst]
    return max(analyst or found, key=lambda d: d.at)


def ingest_floor() -> Optional[int]:
    """elastic-siem's min_rule_level when its ingestion drops alerts below it, else None.

    Only the direct-indexer path (no kibana_url) filters on rule.level.
    """
    from core.detections.inventory.wazuh_sees import indexer
    from core.integrations.elastic.ingestion import min_rule_level_from_config

    why, config = indexer()
    if why or config.get("kibana_url"):
        return None
    return min_rule_level_from_config(config)


def _below(level: Any, floor: Optional[int]) -> bool:
    # A missing or unreadable level is no floor: never guess.
    return floor is not None and type(level) is int and level < floor


def result_of(
    counts: Dict[str, int], level: Any = None, floor: Optional[int] = None
) -> Dict[str, Any]:
    real, judged = counts["real"], counts["judged"]
    split = f"{real} of {judged} real (analyst {counts['analyst']}, agent {counts['agent']})"
    if judged < MIN_JUDGED and _below(level, floor):
        result = "unknown"
        reason = (
            f"unknown: alerts below the ingestion floor (level {level} < {floor})"
            f" are not ingested; {split}"
        )
    elif judged < MIN_JUDGED:
        result = "unknown"
        reason = f"unknown: not enough data: {judged} of {MIN_JUDGED}; {split}"
    else:
        result = "fail" if real < FAIL_BELOW * judged else "pass"
        reason = f"{result}: {split}"
    return {"result": result, "reason": reason, **counts}


def _sources(sources: Optional[Sequence[str]]) -> List[Any]:
    """``None`` keeps every data source (an overlay copy's Wazuh findings included)."""
    if sources is None:
        return []
    return [Finding.data_source.in_(tuple(sources))]


def _findings(
    session: Any, *where: Any, sources: Optional[Sequence[str]] = FINDING_SOURCES
) -> Dict[str, Dict[str, Any]]:
    """Findings matching ``where``, as much as resolving and a noise mark need.

    ``entity_context`` is only ``source_evidence``, the same slice ``judge`` resolves.
    A named overlay rule is not in it.
    """
    rows = session.execute(
        select(
            Finding.finding_id,
            Finding.data_source,
            Finding.timestamp,
            Finding.entity_context["source_evidence"],
            Finding.noise_marked_at,
            Finding.noise_marked_by_kind,
            Finding.noise_reason,
            Finding.noise_cause,
        ).where(*_sources(sources), *where)
    ).all()
    return {
        fid: {
            "data_source": source,
            "timestamp": ts,
            "entity_context": {"source_evidence": evidence},
            "noise": (marked, kind, reason, cause),
        }
        for fid, source, ts, evidence, marked, kind, reason, cause in rows
    }


def _closes(
    session: Any, *where: Any, sources: Optional[Sequence[str]] = FINDING_SOURCES
) -> Dict[str, List[Determination]]:
    """Each finding's (matching ``where``) closed-Case determinations."""
    out: Dict[str, List[Determination]] = {}
    for (
        fid,
        category,
        channel,
        closed_at,
        fp_reason,
        notes,
        fp_cause,
    ) in session.execute(
        select(
            case_findings.c.finding_id,
            CaseClosureInfo.closure_category,
            CaseClosureInfo.close_channel,
            CaseClosureInfo.closed_at,
            CaseClosureInfo.false_positive_reason,
            CaseClosureInfo.closure_notes,
            CaseClosureInfo.false_positive_cause,
        )
        .join(
            Case, and_(Case.case_id == case_findings.c.case_id, Case.status == "closed")
        )
        .join(CaseClosureInfo, CaseClosureInfo.case_id == Case.case_id)
        .join(Finding, Finding.finding_id == case_findings.c.finding_id)
        .where(*_sources(sources), *where)
    ):
        real = closure_says_real(category)
        if real is None:
            continue
        out.setdefault(fid, []).append(
            Determination(
                _utc(closed_at),
                closed_by_person(channel),
                real,
                None if real else _known_cause(fp_cause),
                True,
                "" if real else _free_text(fp_reason, notes),
            )
        )
    return out


def determination_of(
    closes: List[Determination],
    marked: Optional[datetime],
    kind: Optional[str],
    reason: Optional[str] = None,
    cause: Optional[str] = None,
) -> Optional[Determination]:
    """The winning determination, noise mark included, or None."""
    found = list(closes)
    if marked is not None:
        found.append(
            Determination(
                _utc(marked),
                kind == ClosedByKind.ANALYST.value,
                False,
                _known_cause(cause),
                False,
                _free_text(reason),
            )
        )
    return _winning(found)


def verdict_of(
    closes: List[Determination],
    marked: Optional[datetime],
    kind: Optional[str],
    reason: Optional[str] = None,
    cause: Optional[str] = None,
) -> Optional[Tuple[bool, bool]]:
    """(real, analyst) of one finding from its closes and noise mark, or None."""
    won = determination_of(closes, marked, kind, reason, cause)
    if won is None:
        return None
    return won.real, won.analyst


def verdicts(session: Any, *where: Any) -> Dict[str, Optional[Tuple[bool, bool]]]:
    """Each elastic or splunk finding matching ``where``: its verdict_of."""
    closes = _closes(session, *where)
    rows = session.execute(
        select(
            Finding.finding_id, Finding.noise_marked_at, Finding.noise_marked_by_kind
        ).where(Finding.data_source.in_(FINDING_SOURCES), *where)
    )
    return {
        fid: verdict_of(closes.get(fid, []), marked, kind) for fid, marked, kind in rows
    }


def judge(session: Any) -> Tuple[Dict[Tuple[int, int], Dict[str, int]], int]:
    """Per (rule id, latest version) of an active Kibana, Wazuh or Splunk rule, its counts;
    and how many findings resolved to one."""
    latest = {
        (rid, ver): dict.fromkeys(COUNTS, 0)
        for rid, ver in session.execute(
            select(DetectionRule.id, DetectionRule.latest_version).where(
                DetectionRule.source.in_(catalog.RESOLVES),
                DetectionRule.status == "active",
            )
        )
    }
    findings = _findings(session)
    resolved = catalog.resolve_findings(session, findings, full=False)
    closes = _closes(session)
    counted = 0
    for fid, rule in resolved.items():
        key = (rule.get("rule_id"), rule.get("version"))
        if not rule["resolved"] or key not in latest:
            continue
        counted += 1
        won = determination_of(closes.get(fid, []), *findings[fid]["noise"])
        if won is None:
            continue
        counts = latest[key]
        counts["judged"] += 1
        counts["real"] += won.real
        counts["analyst" if won.analyst else "agent"] += 1
        if won.real:
            continue
        counts[won.cause or "not_given"] += 1
        counts["false_person" if won.analyst else "false_agent"] += 1
        if won.case_close:
            counts["case_close"] += 1
    return latest, counted


def _entry(finding_id: str, won: Determination) -> Dict[str, Any]:
    return {
        "finding_id": finding_id,
        "text": won.text,
        "person": won.analyst,
        "cause": won.cause,
    }


def _false_of(session: Any, rule: Any) -> List[Dict[str, Any]]:
    """One rule's judged-false determinations, newest first.

    A Kibana, Wazuh or Splunk rule uses the same resolution as ``judge``: latest
    version, ``catalog.RESOLVES``. An overlay copy takes findings that name it,
    any version, including a Wazuh copy's ``wazuh`` findings. A repo rule has none.
    """
    if rule.source in catalog.RESOLVES:
        findings = _findings(session)
        resolved = catalog.resolve_findings(session, findings, full=False)
        closes = _closes(session)
        picked = []
        for fid, mapped in resolved.items():
            if (
                not mapped.get("resolved")
                or mapped.get("rule_id") != rule.id
                or mapped.get("version") != rule.latest_version
            ):
                continue
            won = determination_of(closes.get(fid, []), *findings[fid]["noise"])
            if won is not None and not won.real:
                picked.append((fid, won))
    elif rule.source == OVERLAY_SOURCE:
        named = Finding.entity_context["detection_rule"].contains({"rule_id": rule.id})
        findings = _findings(session, named, sources=None)
        closes = _closes(session, named, sources=None)
        picked = []
        for fid, found in findings.items():
            won = determination_of(closes.get(fid, []), *found["noise"])
            if won is not None and not won.real:
                picked.append((fid, won))
    else:
        picked = []
    picked.sort(key=lambda item: (item[1].at, item[0]), reverse=True)
    return [_entry(fid, won) for fid, won in picked]


def judged_false_page(rule_id: int, offset: int, limit: int) -> Dict[str, Any]:
    """A page of one rule's judged-false determinations. Empty when it has none."""
    offset = max(int(offset), 0)
    limit = min(max(int(limit), 1), FALSE_PAGE)
    with get_db_manager().session_scope() as session:
        rule = session.get(DetectionRule, rule_id)
        entries = _false_of(session, rule) if rule is not None else []
    page = entries[offset : offset + limit]
    end = offset + len(page)
    more = end < len(entries)
    return {
        "total": len(entries),
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "has_more": more,
        **({"next_offset": end} if more else {}),
        "entries": page,
    }


def sweep() -> AccurateReport:
    """Judge every active Kibana, Wazuh and Splunk rule's latest version and write what changed."""
    from core.detections.inventory.sync import _change, _last_results, _record_changes

    report = AccurateReport()
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_manager().session_scope() as session:
            floor = ingest_floor()
            counts, report.findings = judge(session)
            report.rules = len(counts)
            report.judged = sum(c["judged"] for c in counts.values())
            last: Dict[tuple, tuple] = {}
            for source in catalog.RESOLVES:
                last.update(_last_results(session, source))
            rows = session.execute(
                select(
                    DetectionRuleVersion.rule_id,
                    DetectionRuleVersion.version,
                    DetectionRuleVersion.checks,
                    DetectionRule.source,
                    DetectionRuleVersion.content["level"],
                )
                .join(DetectionRule, DetectionRule.id == DetectionRuleVersion.rule_id)
                .where(
                    tuple_(
                        DetectionRuleVersion.rule_id, DetectionRuleVersion.version
                    ).in_(list(counts))
                )
            ).all()
            changed, changes = [], []
            for rid, ver, checks, source, level in rows:
                wazuh_level = level if source == "wazuh" else None
                new = result_of(counts[(rid, ver)], wazuh_level, floor)
                old = (checks or {}).get("accurate") or {}
                seen = (new["result"], new["reason"])
                before = last.get((rid, "accurate"))
                if before != seen:
                    changes.append(
                        _change(
                            rid, ver, "accurate", before[0] if before else None, *seen
                        )
                    )
                if all(old.get(k) == v for k, v in new.items()):
                    continue
                changed.append(
                    {
                        "rid": rid,
                        "ver": ver,
                        "new_checks": {
                            **(checks or {}),
                            "accurate": {**new, "checked_at": now},
                        },
                    }
                )
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
            _record_changes(session, changes, now)
            report.written = len(changed)
    except Exception as exc:  # noqa: BLE001 -- the sync's own reports stand
        logger.warning("detection rules: Accurate sweep failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Accurate %s", report)
    return report
