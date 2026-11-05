"""Accurate: the share of each Kibana, Wazuh and Splunk rule's alerts judged real.

Each elastic or splunk finding is resolved to its rule version with
``rule_for_finding``'s own mapping (``catalog.resolve_findings``). Only findings
of the latest version of an active rule of a ``catalog.ACCURATE_TOOLS`` source
count, with no time cap.

A finding's determinations are its noise mark (not real) and each closed Case
it is in whose closure category CLOSURE_OUTCOMES maps to proven (real) or
false_positive (not real); an open or reopened Case, or an inconclusive or
duplicate close, determines nothing. The latest analyst determination wins,
else the latest agent one. A noise mark is the analyst's when its kind says so;
a close is when ``closed_by_person`` says so.

Fewer than MIN_JUDGED judged reads unknown; under FAIL_BELOW real reads fail;
else pass. The result goes under checks['accurate'] on the latest version,
written only when result, reason or counts changed; a result or reason change is
also appended to detection_rule_check_changes. Local SQL only: no SIEM search.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, bindparam, select, tuple_, update

from core.cases.closure import ClosedByKind, closed_by_person, closure_says_real
from core.detections.inventory import catalog
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
COUNTS = ("real", "judged", "analyst", "agent")

# (at, analyst, real): one determination of a finding.
Determination = Tuple[datetime, bool, bool]


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


def _verdict(found: List[Determination]) -> Optional[Tuple[bool, bool]]:
    """(real, analyst) of the latest analyst determination, else the latest agent one."""
    if not found:
        return None
    analyst = [d for d in found if d[1]]
    _, by_analyst, real = max(analyst or found, key=lambda d: d[0])
    return real, by_analyst


def result_of(counts: Dict[str, int]) -> Dict[str, Any]:
    real, judged = counts["real"], counts["judged"]
    split = f"{real} of {judged} real (analyst {counts['analyst']}, agent {counts['agent']})"
    if judged < MIN_JUDGED:
        result = "unknown"
        reason = f"unknown: not enough data: {judged} of {MIN_JUDGED}; {split}"
    else:
        result = "fail" if real < FAIL_BELOW * judged else "pass"
        reason = f"{result}: {split}"
    return {"result": result, "reason": reason, **counts}


def _findings(session: Any) -> Dict[str, Dict[str, Any]]:
    """Every elastic or splunk finding, as much as resolving and its noise mark need."""
    rows = session.execute(
        select(
            Finding.finding_id,
            Finding.data_source,
            Finding.timestamp,
            Finding.entity_context["source_evidence"],
            Finding.noise_marked_at,
            Finding.noise_marked_by_kind,
        ).where(Finding.data_source.in_(FINDING_SOURCES))
    ).all()
    return {
        fid: {
            "data_source": source,
            "timestamp": ts,
            "entity_context": {"source_evidence": evidence},
            "noise": (marked, kind),
        }
        for fid, source, ts, evidence, marked, kind in rows
    }


def _closes(session: Any) -> Dict[str, List[Determination]]:
    """Each elastic or splunk finding's closed-Case determinations (as _CLOSURE_VERDICTS)."""
    out: Dict[str, List[Determination]] = {}
    for fid, category, channel, closed_at in session.execute(
        select(
            case_findings.c.finding_id,
            CaseClosureInfo.closure_category,
            CaseClosureInfo.close_channel,
            CaseClosureInfo.closed_at,
        )
        .join(
            Case, and_(Case.case_id == case_findings.c.case_id, Case.status == "closed")
        )
        .join(CaseClosureInfo, CaseClosureInfo.case_id == Case.case_id)
        .join(Finding, Finding.finding_id == case_findings.c.finding_id)
        .where(Finding.data_source.in_(FINDING_SOURCES))
    ):
        real = closure_says_real(category)
        if real is not None:
            out.setdefault(fid, []).append(
                (_utc(closed_at), closed_by_person(channel), real)
            )
    return out


def judge(session: Any) -> Tuple[Dict[Tuple[int, int], Dict[str, int]], int]:
    """Per (rule id, latest version) of an active Kibana, Wazuh or Splunk rule, its counts;
    and how many findings resolved to one."""
    latest = {
        (rid, ver): dict.fromkeys(COUNTS, 0)
        for rid, ver in session.execute(
            select(DetectionRule.id, DetectionRule.latest_version).where(
                DetectionRule.source.in_(catalog.ACCURATE_TOOLS),
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
        found = list(closes.get(fid, []))
        marked, kind = findings[fid]["noise"]
        if marked is not None:
            found.append((_utc(marked), kind == ClosedByKind.ANALYST.value, False))
        verdict = _verdict(found)
        if verdict is None:
            continue
        real, analyst = verdict
        counts = latest[key]
        counts["judged"] += 1
        counts["real"] += real
        counts["analyst" if analyst else "agent"] += 1
    return latest, counted


def sweep() -> AccurateReport:
    """Judge every active Kibana, Wazuh and Splunk rule's latest version and write what changed."""
    from core.detections.inventory.sync import _change, _last_results, _record_changes

    report = AccurateReport()
    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db_manager().session_scope() as session:
            counts, report.findings = judge(session)
            report.rules = len(counts)
            report.judged = sum(c["judged"] for c in counts.values())
            last: Dict[tuple, tuple] = {}
            for source in catalog.ACCURATE_TOOLS:
                last.update(_last_results(session, source))
            rows = session.execute(
                select(
                    DetectionRuleVersion.rule_id,
                    DetectionRuleVersion.version,
                    DetectionRuleVersion.checks,
                ).where(
                    tuple_(
                        DetectionRuleVersion.rule_id, DetectionRuleVersion.version
                    ).in_(list(counts))
                )
            ).all()
            changed, changes = [], []
            for rid, ver, checks in rows:
                new = result_of(counts[(rid, ver)])
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
