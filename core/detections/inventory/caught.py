"""Caught: whether a rule version alerted on a real attack step.

The evidence is Vigil's own, over the last LOOKBACK_DAYS: compose runs scored
as ``learning_episodes`` scores them (each technique row's cited findings), and
proven hunt and Case Verdicts, each one step reconstructed as
``gaps.missed_runs_report`` does (a cut-off Findings read gives no credit).
LogLM and probe citations are not a rule alerting and are skipped.

Each cited finding resolves to the version that fired it through
``catalog.resolve_findings``, which picks the version first seen at or before
the alert, so a version is never credited on alerts from before it existed.
It passes only when its rule declares the step's technique, at parent level
(T1059.001 ~ T1059): correlation is host, IP or user plus time, so without it
any noisy rule on the host would pass.

Only ``pass`` is written, under checks['caught'], and only on versions not
already passing; it is never cleared and there is no fail. A version with no
result is untested. The sweep runs at most once per HOLD_SECONDS per process
(it reads Vigil's own records and run projections: no SIEM search, no budget).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import select, tuple_

from core.detections import gaps
from core.detections.inventory import catalog, sees
from core.detections.reconstruction import _LOG_LM
from core.storage.connection import get_db_manager
from core.storage.models import DetectionRule, DetectionRuleVersion, Finding

logger = logging.getLogger(__name__)

CHECK = "caught"
LOOKBACK_DAYS = 30
HOLD_SECONDS = 24 * 3600.0
_SKIPPED = (_LOG_LM, "probe")

# When this process last finished a sweep (time.monotonic()).
_last_swept: Optional[float] = None


@dataclass
class Evidence:
    """One attack step: when, its techniques, what names it, the findings cited on it."""

    at: datetime
    techniques: Set[str]
    label: str
    finding_ids: List[str]


@dataclass
class CaughtReport:
    source: str = "caught"
    runs: int = 0
    verdicts: int = 0
    findings: int = 0
    written: int = 0
    held: bool = False
    error: Optional[str] = None


def _parent(technique: str) -> str:
    return technique.strip().upper().split(".")[0]


def _cited(steps: Iterable[Any]) -> List[str]:
    return [
        str(c["finding_id"])
        for step in steps
        if isinstance(step, dict)
        for c in step.get("citations") or []
        if isinstance(c, dict)
        and c.get("finding_id")
        and c.get("data_source") not in _SKIPPED
    ]


def _utc(value: Any) -> datetime:
    when = catalog._when(value)
    return when or datetime.min.replace(tzinfo=timezone.utc)


def _from_runs(start: datetime, end: datetime) -> List[Evidence]:
    """Each technique row of each finished compose run's coverage report."""
    from core.memory.learning_episodes import emulation_episodes

    out = []
    for episode in asyncio.run(emulation_episodes(start, end)):
        at = _utc(episode.get("concluded_at"))
        run_id = episode["investigation_id"]
        for row in episode["payload"].get("techniques") or []:
            out.append(
                Evidence(
                    at,
                    {row["technique_id"]},
                    f"compose run {run_id}",
                    _cited(row.get("steps") or []),
                )
            )
    return out


def _from_verdicts(since_days: int) -> List[Evidence]:
    """Each proven Verdict as one step, as ``missed_runs_report`` reconstructs it."""
    from core.storage.database_data_service import DatabaseDataService

    data = DatabaseDataService()

    def read(start: datetime, end: datetime) -> List[Dict[str, Any]]:
        return data.get_findings(
            limit=gaps.FINDINGS_LIMIT, timestamp_start=start, timestamp_end=end
        )

    out = []
    for v in gaps.read_ledger(since_days):
        if v["outcome"] != "proven" or not v["techniques"]:
            continue
        why, record = gaps.reconstruct_verdict(v, read)
        if why:
            continue
        kind = "Case" if v["investigation_kind"] == "case" else "hunt"
        out.append(
            Evidence(
                _utc(v["concluded_at"]),
                set(v["techniques"]),
                f"{kind} {v['investigation_id']} (proven, {v['trust']})",
                _cited([record]),
            )
        )
    return out


def judge(
    session: Any, evidence: List[Evidence]
) -> Dict[Tuple[int, int], Tuple[str, Dict[str, Any]]]:
    """Per (rule id, version) newly caught: its rule's source and the pass result.

    The newest evidence names a version; one already passing is left out.
    """
    ids = {fid for e in evidence for fid in e.finding_ids}
    if not ids:
        return {}
    findings = {
        fid: {
            "data_source": source,
            "timestamp": ts,
            "entity_context": {"source_evidence": found, "detection_rule": named},
        }
        for fid, source, ts, found, named in session.execute(
            select(
                Finding.finding_id,
                Finding.data_source,
                Finding.timestamp,
                Finding.entity_context["source_evidence"],
                Finding.entity_context["detection_rule"],
            ).where(Finding.finding_id.in_(ids))
        )
    }
    resolved = {
        fid: (r["rule_id"], r["version"])
        for fid, r in catalog.resolve_findings(session, findings, full=False).items()
        if r["resolved"]
    }
    versions = {
        (rid, ver): (source, {_parent(t) for t in techniques or []}, caught)
        for rid, ver, source, techniques, caught in session.execute(
            select(
                DetectionRuleVersion.rule_id,
                DetectionRuleVersion.version,
                DetectionRule.source,
                DetectionRule.techniques,
                catalog.CAUGHT,
            )
            .join(DetectionRule, DetectionRule.id == DetectionRuleVersion.rule_id)
            .where(
                tuple_(DetectionRuleVersion.rule_id, DetectionRuleVersion.version).in_(
                    set(resolved.values())
                )
            )
        )
    }
    out: Dict[Tuple[int, int], Tuple[str, Dict[str, Any]]] = {}
    for e in sorted(evidence, key=lambda e: e.at, reverse=True):
        for fid in e.finding_ids:
            key = resolved.get(fid)
            if key is None or key in out or key not in versions:
                continue
            source, declared, caught = versions[key]
            if caught == "pass":
                continue
            technique = next(
                (t for t in sorted(e.techniques) if _parent(t) in declared), None
            )
            if technique is None:
                continue
            day = f", {e.at:%Y-%m-%d}" if e.at.year > 1 else ""
            out[key] = (
                source,
                {
                    "result": "pass",
                    "reason": f"pass: caught in {e.label}, {technique}{day}",
                },
            )
    return out


def sweep() -> CaughtReport:
    """Credit each rule version cited on a matching attack step; at most once per hold."""
    global _last_swept
    report = CaughtReport()
    if _last_swept is not None and time.monotonic() - _last_swept < HOLD_SECONDS:
        report.held = True
        return report
    try:
        end = datetime.now(timezone.utc)
        runs = _from_runs(end - timedelta(days=LOOKBACK_DAYS), end)
        verdicts = _from_verdicts(LOOKBACK_DAYS)
        report.runs = len({e.label for e in runs})
        report.verdicts = len(verdicts)
        evidence = runs + verdicts
        report.findings = len({fid for e in evidence for fid in e.finding_ids})
        with get_db_manager().session_scope() as session:
            caught = judge(session, evidence)
        by_source: Dict[str, Dict[Tuple[int, int], Dict[str, Any]]] = {}
        for key, (source, result) in caught.items():
            by_source.setdefault(source, {})[key] = result
        for source, results in by_source.items():
            report.written += sees.write_check(CHECK, results, source=source)
        _last_swept = time.monotonic()
    except Exception as exc:  # noqa: BLE001 -- the sync's own reports stand
        logger.warning("detection rules: Caught sweep failed: %s", exc)
        report.error = f"{type(exc).__name__}: {exc}"
    logger.info("detection rules: Caught %s", report)
    return report
