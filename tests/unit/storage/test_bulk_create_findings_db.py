"""bulk_create_findings keeps the valid rows when one row in a batch fails (#1434).

PostgreSQL-backed: SQLite does not enforce VARCHAR lengths, so it would hide
the over-length failure these tests exist to exercise.
"""

import pytest
from sqlalchemy import func, select

from core.storage.models import Finding
from core.storage.service import DatabaseService
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _row(finding_id: str, **overrides):
    row = {
        "finding_id": finding_id,
        "anomaly_score": 0.5,
        "timestamp": utcnow(),
        "data_source": "test-bulk",
        "severity": "high",
        "mitre_predictions": {"T1059.001": 0.9},
    }
    row.update(overrides)
    return row


def _count(service: DatabaseService, prefix: str) -> int:
    with service.db_manager.session_scope() as session:
        return session.execute(
            select(func.count())
            .select_from(Finding)
            .where(Finding.finding_id.like(f"{prefix}%"))
        ).scalar_one()


def test_valid_batch_imports_in_one_pass():
    service = DatabaseService()
    rows = [_row(f"bulk-ok-{i}") for i in range(5)]

    assert service.bulk_create_findings(rows) == {"imported": 5, "skipped": 0}
    assert _count(service, "bulk-ok-") == 5

    # Re-sending the batch skips every row.
    assert service.bulk_create_findings(rows) == {"imported": 0, "skipped": 5}


def test_over_length_finding_id_costs_only_its_own_row():
    service = DatabaseService()
    n = 20
    rows = [_row(f"bulk-len-{i}") for i in range(n)]
    # findings.finding_id is String(50).
    rows.insert(10, _row("bulk-len-" + "x" * 42))

    result = service.bulk_create_findings(rows)

    assert result == {"imported": n, "skipped": 0, "errors": 1}
    assert _count(service, "bulk-len-") == n


def test_over_length_severity_with_existing_and_duplicate_rows():
    service = DatabaseService()
    assert service.bulk_create_findings([_row("bulk-mix-old")])["imported"] == 1

    rows = [
        _row("bulk-mix-0"),
        _row("bulk-mix-old"),  # already stored
        _row("bulk-mix-1", severity="s" * 21),  # severity is String(20)
        _row("bulk-mix-2"),
        _row("bulk-mix-2"),  # repeated inside the batch
    ]

    result = service.bulk_create_findings(rows)

    assert result == {"imported": 2, "skipped": 2, "errors": 1}
    assert service.get_finding("bulk-mix-0") is not None
    assert service.get_finding("bulk-mix-1") is None
    assert service.get_finding("bulk-mix-2") is not None
