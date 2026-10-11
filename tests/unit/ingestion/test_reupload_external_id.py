"""Re-uploading alerts with known external ids skips them, plainly (#1935).

The single-row path checked only finding_id, so a re-upload with new ids
and the same (source, external_id) pairs reached create_finding, which
failed, and the row counted as an error instead of already imported. The
fake db below mirrors the real DatabaseService contract: create returns
None when the row cannot be stored (its decorator swallows the error).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.ingestion.ingestion_service import IngestionService  # noqa: E402

pytestmark = pytest.mark.unit

RAW = "psycopg2 UniqueViolation RAWDRIVER duplicate key"


class _FakeDb:
    """Stores findings and answers both identity lookups, like the real db."""

    def __init__(self):
        self.by_id: dict = {}
        self.by_external: dict = {}

    def get_finding(self, finding_id):
        return self.by_id.get(finding_id)

    def get_finding_by_external_id(self, data_source, external_id):
        return self.by_external.get((data_source, external_id))

    def create_finding(self, finding_id=None, data_source=None, external_id=None, **kw):
        if finding_id in self.by_id:
            return None
        if external_id and (data_source, external_id) in self.by_external:
            return None  # the unique index refuses it; the error is swallowed
        stored = object()
        self.by_id[finding_id] = stored
        if external_id:
            self.by_external[(data_source, external_id)] = stored
        return stored


def _alert(finding_id, external_id, source="okta"):
    return {
        "finding_id": finding_id,
        "data_source": source,
        "external_id": external_id,
        "title": "Suspicious sign-in",
    }


@pytest.fixture
def service():
    svc = IngestionService()
    svc.use_database = True
    svc.db_service = _FakeDb()
    return svc


def test_reupload_with_new_ids_and_same_external_ids_is_all_skipped(service):
    first = [_alert(f"f-{i}", f"okta-900{i}") for i in range(3)]
    for row in first:
        assert service.ingest_finding(row) is True
    assert service.stats["findings_imported"] == 3

    service.stats = {k: 0 for k in service.stats}
    second = [_alert(f"g-{i}", f"okta-900{i}") for i in range(3)]
    for row in second:
        assert service.ingest_finding(row) is True

    assert service.stats["findings_imported"] == 0
    assert service.stats["findings_skipped"] == 3
    assert service.stats["findings_errors"] == 0
    assert service.first_error is None


def test_same_external_id_from_another_source_is_imported(service):
    assert service.ingest_finding(_alert("f-1", "okta-9001")) is True
    assert service.ingest_finding(_alert("f-2", "okta-9001", source="splunk")) is True

    assert service.stats["findings_imported"] == 2
    assert service.stats["findings_skipped"] == 0


def test_a_row_without_an_external_id_is_imported_normally(service):
    assert service.ingest_finding(_alert("f-1", None)) is True
    assert service.stats["findings_imported"] == 1


def test_a_database_failure_is_recorded_in_plain_language(service, caplog):
    class _DownDb(_FakeDb):
        def get_finding(self, finding_id):
            raise RuntimeError(RAW)

    service.db_service = _DownDb()

    assert service.ingest_finding(_alert("f-1", "okta-9001")) is False

    assert service.stats["findings_errors"] == 1
    assert service.first_error is not None
    assert "psycopg2" not in service.first_error
    assert "RAWDRIVER" not in service.first_error
    # The full driver detail is in the log, and only there.
    assert "RAWDRIVER" in caplog.text
