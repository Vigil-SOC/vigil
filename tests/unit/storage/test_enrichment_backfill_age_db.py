"""The backfill sweep's age bound is when Vigil stored the finding (#2320).

Filtering on the event ``timestamp`` skipped replayed captures and undated
findings forever; the bound is ``created_at`` instead.
"""

import uuid
from datetime import timedelta

import pytest

from core.storage.connection import get_db_manager
from core.storage.models import Finding
from core.storage.service import DatabaseService
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _finding(service: DatabaseService, timestamp, created_days_ago: int = 0) -> str:
    finding_id = f"bf-age-{uuid.uuid4().hex[:12]}"
    assert service.create_finding(
        finding_id=finding_id,
        mitre_predictions={},
        anomaly_score=0.5,
        timestamp=timestamp,
        data_source="backfill-age",
        severity="medium",
        status="new",
    )
    if created_days_ago:
        with get_db_manager().session_scope() as session:
            session.get(Finding, finding_id).created_at = utcnow() - timedelta(
                days=created_days_ago
            )
    return finding_id


def _missing(service: DatabaseService) -> set:
    return {
        f["finding_id"]
        for f in service.get_findings_missing_enrichment(
            limit=100000, max_age_hours=168
        )
    }


def test_backfill_age_is_measured_from_storage_not_event_time():
    service = DatabaseService()
    replayed = _finding(service, utcnow() - timedelta(days=30))
    undated = _finding(service, None)
    stale = _finding(service, utcnow(), created_days_ago=8)
    failed = _finding(service, utcnow())
    assert service.update_finding(failed, ai_enrichment={"ai_triage_error": "boom"})

    missing = _missing(service)
    assert {replayed, undated, failed} <= missing
    assert stale not in missing
