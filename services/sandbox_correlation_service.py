"""Correlate completed sandbox reports into case evidence + IOCs.

Sandbox reports (CAPE, Joe, Hybrid Analysis, Any.Run) are large JSON blobs.
This service normalises their salient parts into Vigil's existing
``CaseEvidence`` and ``CaseIOC`` tables so they show up on the case view
alongside human-entered evidence, without needing a new schema.

Design notes
------------
- ``CaseEvidence.analysis_results`` (JSONB) is the natural home for the raw
  sandbox report. We also populate ``file_hash_*`` when the report exposes a
  primary sample hash.
- ``CaseIOC`` is used for extracted IOCs (network IPs/domains/URLs, dropped
  file hashes, mutexes). Per-IOC ``enrichment_data`` captures which sandbox
  report it came from, for back-reference.
- ``reputation_score`` is derived heuristically from the sandbox verdict
  score where present.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from database.models import CaseEvidence, CaseIOC
from database.connection import get_db_session

logger = logging.getLogger(__name__)


class SandboxCorrelationService:
    """Attach a sandbox report to a case as evidence + IOCs."""

    def __init__(self) -> None:
        pass

    def attach_report(
        self,
        case_id: str,
        sandbox_name: str,
        task_id: str,
        report: Dict[str, Any],
        collected_by: str = "sandbox-poller",
        session: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """Insert one ``CaseEvidence`` row + N ``CaseIOC`` rows.

        Returns a small summary dict for caller logging.
        """
        should_close = session is None
        if session is None:
            session = get_db_session()

        try:
            normalised = _normalise_report(sandbox_name, report)
            evidence = CaseEvidence(
                case_id=case_id,
                evidence_type="sandbox_report",
                name=f"{sandbox_name} report {task_id}",
                description=f"Automated sandbox detonation from {sandbox_name}",
                file_path=None,
                file_size=None,
                file_hash_md5=normalised.get("md5"),
                file_hash_sha256=normalised.get("sha256"),
                source=sandbox_name,
                collected_by=collected_by,
                collected_at=datetime.utcnow(),
                chain_of_custody=[
                    {
                        "timestamp": datetime.utcnow().isoformat(),
                        "action": "collected",
                        "user": collected_by,
                        "notes": f"Retrieved from {sandbox_name} task {task_id}",
                    }
                ],
                analysis_results={
                    "sandbox": sandbox_name,
                    "task_id": task_id,
                    "verdict": normalised.get("verdict"),
                    "score": normalised.get("score"),
                    "mitre_techniques": normalised.get("mitre_techniques", []),
                    "raw": report,
                },
                tags=["sandbox", sandbox_name],
            )
            session.add(evidence)
            session.flush()

            iocs_added = 0
            for ioc_type, value in _iter_iocs(normalised.get("iocs", {})):
                if self._upsert_ioc(
                    session=session,
                    case_id=case_id,
                    ioc_type=ioc_type,
                    value=value,
                    source=sandbox_name,
                    sandbox_task_id=task_id,
                    score=normalised.get("score"),
                ):
                    iocs_added += 1

            session.commit()
            return {
                "evidence_id": evidence.evidence_id,
                "iocs_added": iocs_added,
                "verdict": normalised.get("verdict"),
            }
        except Exception as e:
            session.rollback()
            logger.exception("Failed to correlate sandbox report")
            return {"error": str(e)}
        finally:
            if should_close:
                session.close()

    # ---------- internals ----------

    def _upsert_ioc(
        self,
        session: Session,
        case_id: str,
        ioc_type: str,
        value: str,
        source: str,
        sandbox_task_id: str,
        score: Optional[float],
    ) -> bool:
        if not value:
            return False
        existing = (
            session.query(CaseIOC)
            .filter(
                CaseIOC.case_id == case_id,
                CaseIOC.ioc_type == ioc_type,
                CaseIOC.value == value,
            )
            .first()
        )
        now = datetime.utcnow()
        enrichment_chunk = {
            "sandbox": source,
            "task_id": sandbox_task_id,
            "observed_at": now.isoformat(),
        }
        if existing:
            existing.last_seen = now
            merged = dict(existing.enrichment_data or {})
            runs = list(merged.get("sandbox_runs") or [])
            runs.append(enrichment_chunk)
            merged["sandbox_runs"] = runs[-10:]
            existing.enrichment_data = merged
            if score is not None:
                existing.reputation_score = max(
                    existing.reputation_score or 0.0, float(score)
                )
            return False

        ioc = CaseIOC(
            case_id=case_id,
            ioc_type=ioc_type,
            value=value,
            threat_level=_score_to_threat_level(score),
            confidence=_score_to_confidence(score),
            source=source,
            first_seen=now,
            last_seen=now,
            enrichment_data={"sandbox_runs": [enrichment_chunk]},
            reputation_score=float(score) if score is not None else None,
            tags=["sandbox", source],
            is_active=True,
            is_false_positive=False,
        )
        session.add(ioc)
        return True


# ---------- pure helpers (unit-testable without a DB) ----------


def _normalise_report(sandbox_name: str, report: Dict[str, Any]) -> Dict[str, Any]:
    """Pick a common subset of fields across sandbox vendors."""
    sandbox = sandbox_name.lower()
    if sandbox in ("cape", "cape_sandbox", "cape-sandbox"):
        return _normalise_cape(report)
    if sandbox in ("joe", "joe_sandbox", "joe-sandbox"):
        return _normalise_joe(report)
    if sandbox in ("hybrid", "hybrid_analysis", "hybrid-analysis"):
        return _normalise_hybrid(report)
    if sandbox in ("anyrun", "any.run"):
        return _normalise_anyrun(report)
    # Unknown — best effort
    return {"iocs": {}, "mitre_techniques": [], "verdict": None, "score": None}


def _normalise_cape(report: Dict[str, Any]) -> Dict[str, Any]:
    analysis = report.get("data") if isinstance(report.get("data"), dict) else report
    target = (analysis.get("target") or {}).get("file", {}) or {}
    info = analysis.get("info") or {}
    malscore = info.get("score")
    signatures = analysis.get("signatures") or []

    iocs: Dict[str, List[str]] = {
        "ip": [],
        "domain": [],
        "url": [],
        "hash": [],
        "mutex": [],
    }
    network = analysis.get("network") or {}
    for host in network.get("hosts", []) or []:
        ip = host.get("ip") if isinstance(host, dict) else host
        if ip:
            iocs["ip"].append(ip)
    for dns in network.get("dns", []) or []:
        req = dns.get("request") if isinstance(dns, dict) else None
        if req:
            iocs["domain"].append(req)
    for http in network.get("http", []) or []:
        u = http.get("uri") if isinstance(http, dict) else None
        if u:
            iocs["url"].append(u)
    for dropped in analysis.get("dropped", []) or []:
        if isinstance(dropped, dict):
            sha = dropped.get("sha256") or dropped.get("sha1") or dropped.get("md5")
            if sha:
                iocs["hash"].append(sha)
    for mx in (analysis.get("behavior", {}).get("summary", {}) or {}).get(
        "mutexes", []
    ) or []:
        iocs["mutex"].append(mx)

    mitre = []
    for sig in signatures:
        for tech in sig.get("ttp", []) or []:
            if isinstance(tech, str) and tech.upper().startswith("T"):
                mitre.append(tech)

    return {
        "md5": target.get("md5"),
        "sha256": target.get("sha256"),
        "score": malscore,
        "verdict": _cape_verdict(malscore),
        "mitre_techniques": sorted(set(mitre)),
        "iocs": iocs,
    }


def _normalise_joe(report: Dict[str, Any]) -> Dict[str, Any]:
    analysis = report.get("analysis") or report
    score = analysis.get("detection", {}).get("score")
    verdict = analysis.get("detection", {}).get("category")
    return {
        "md5": analysis.get("md5"),
        "sha256": analysis.get("sha256"),
        "score": score,
        "verdict": verdict,
        "mitre_techniques": analysis.get("mitre", []) or [],
        "iocs": _flatten_iocs(analysis.get("iocs", {})),
    }


def _normalise_hybrid(report: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "md5": report.get("md5"),
        "sha256": report.get("sha256"),
        "score": report.get("threat_score"),
        "verdict": report.get("verdict"),
        "mitre_techniques": report.get("mitre_attcks", []) or [],
        "iocs": _flatten_iocs(
            {
                "ip": report.get("hosts", []) or [],
                "domain": report.get("domains", []) or [],
            }
        ),
    }


def _normalise_anyrun(report: Dict[str, Any]) -> Dict[str, Any]:
    data = report.get("data") or report
    return {
        "md5": (data.get("sample") or {}).get("md5"),
        "sha256": (data.get("sample") or {}).get("sha256"),
        "score": data.get("scores", {}).get("malconf"),
        "verdict": data.get("verdict"),
        "mitre_techniques": [],
        "iocs": _flatten_iocs(data.get("iocs", {})),
    }


def _flatten_iocs(raw: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {
        "ip": [],
        "domain": [],
        "url": [],
        "hash": [],
        "mutex": [],
    }
    for key, bucket in (raw or {}).items():
        norm = key.lower()
        if norm in out and isinstance(bucket, list):
            for item in bucket:
                if isinstance(item, str):
                    out[norm].append(item)
                elif isinstance(item, dict):
                    val = item.get("value") or item.get("address") or item.get("url")
                    if val:
                        out[norm].append(val)
    return out


def _iter_iocs(iocs: Dict[str, List[str]]):
    seen = set()
    for ioc_type, values in iocs.items():
        for v in values or []:
            key = (ioc_type, v)
            if key in seen or not v:
                continue
            seen.add(key)
            yield ioc_type, v


def _cape_verdict(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s >= 8:
        return "malicious"
    if s >= 5:
        return "suspicious"
    if s > 0:
        return "benign"
    return "clean"


def _score_to_threat_level(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    if s >= 8:
        return "critical"
    if s >= 6:
        return "high"
    if s >= 3:
        return "medium"
    return "low"


def _score_to_confidence(score: Optional[float]) -> Optional[float]:
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, s / 10.0))
