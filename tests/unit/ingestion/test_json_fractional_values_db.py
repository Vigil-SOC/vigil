"""JSON uploads with fractional numbers in JSONB fields are stored (#3087).

Runs on the throwaway database tests/unit/conftest.py provisions per process.
"""

import json

import pytest
from sqlalchemy import func, select

from core.ingestion.ingestion_service import IngestionService
from core.storage.connection import get_db_session
from core.storage.models import Case, Finding

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _count(model, column, prefix):
    session = get_db_session()
    try:
        return session.execute(
            select(func.count()).select_from(model).where(column.like(f"{prefix}%"))
        ).scalar()
    finally:
        session.close()


@pytest.mark.parametrize("shape", ["dict", "list"])
def test_findings_with_fractional_jsonb_values_are_stored(tmp_path, shape):
    prefix = f"jfv-{shape}-"
    findings = [
        {
            "finding_id": f"{prefix}{i}",
            "data_source": "test",
            "timestamp": "2026-01-01T00:00:00Z",
            "entity_context": {"risk": 0.5},
            "evidence_links": [{"score": 0.25}],
        }
        for i in range(3)
    ]
    path = tmp_path / "upload.json"
    path.write_text(json.dumps({"findings": findings} if shape == "dict" else findings))

    stats = IngestionService().ingest_json_file(path)

    assert stats["findings_errors"] == 0
    assert _count(Finding, Finding.finding_id, prefix) == 3


def test_case_with_fractional_jsonb_value_is_stored(tmp_path):
    prefix = "jfv-case-"
    case = {
        "case_id": f"{prefix}0",
        "title": "fractional",
        "timeline": [{"event": "scored", "score": 0.75}],
    }
    path = tmp_path / "upload.json"
    path.write_text(json.dumps({"findings": [], "cases": [case]}))

    stats = IngestionService().ingest_json_file(path)

    assert stats["cases_errors"] == 0
    assert _count(Case, Case.case_id, prefix) == 1
