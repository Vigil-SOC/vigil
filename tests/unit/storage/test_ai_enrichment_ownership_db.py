"""Daemon triage and the on-demand AI analysis share ``ai_enrichment`` (#2316).

Each writer replaces only its own keys, merged against the stored row, so
neither wipes the other; a finding holding only an analysis stays in the
daemon's backfill.
"""

import uuid

import pytest

from core.storage.service import DatabaseService
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

TRIAGE = {
    "ai_triage": {"result": "SEVERITY: high"},
    "category": "c2",
    "enriched_at": "2026-10-15T00:00:00",
}
ANALYSIS = {"threat_summary": "Beaconing", "generated_at": "2026-10-15T01:00:00"}


def _finding(service: DatabaseService, ai_enrichment: dict) -> str:
    finding_id = f"own-{uuid.uuid4().hex[:12]}"
    assert service.create_finding(
        finding_id=finding_id,
        mitre_predictions={},
        anomaly_score=0.5,
        timestamp=utcnow(),
        data_source="ownership",
        severity="medium",
    )
    assert service.update_finding(finding_id, ai_enrichment=ai_enrichment)
    return finding_id


def _stored(service: DatabaseService, finding_id: str) -> dict:
    return service.get_finding(finding_id).ai_enrichment


def test_analysis_write_keeps_triage():
    service = DatabaseService()
    fid = _finding(service, TRIAGE)
    assert service.write_ai_enrichment(fid, ANALYSIS, daemon=False)
    assert _stored(service, fid) == {**TRIAGE, **ANALYSIS}


def test_triage_write_keeps_analysis():
    service = DatabaseService()
    fid = _finding(service, ANALYSIS)
    assert service.write_ai_enrichment(fid, TRIAGE, daemon=True, severity="high")
    assert _stored(service, fid) == {**ANALYSIS, **TRIAGE}
    assert service.get_finding(fid).severity == "high"


def test_failed_retriage_drops_stale_triage():
    service = DatabaseService()
    fid = _finding(service, {**TRIAGE, **ANALYSIS})
    assert service.write_ai_enrichment(
        fid, {"ai_triage_error": "timed out"}, daemon=True
    )
    assert _stored(service, fid) == {**ANALYSIS, "ai_triage_error": "timed out"}


def test_analysis_only_row_stays_in_backfill():
    service = DatabaseService()
    analysis_only = _finding(service, ANALYSIS)
    triaged = _finding(service, {**TRIAGE, **ANALYSIS})
    failed = _finding(service, {"ai_triage_error": "boom"})
    missing = {
        f["finding_id"]
        for f in service.get_findings_missing_enrichment(limit=1000, max_age_hours=1)
    }
    assert analysis_only in missing and failed in missing
    assert triaged not in missing
