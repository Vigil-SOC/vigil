"""sort_by=created_at orders by arrival, not the source event time.

Runs on the throwaway database tests/unit/conftest.py provisions per process.
"""

from datetime import datetime

import pytest

from core.storage.models import Finding
from core.storage.service import DatabaseService
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

PREFIX = "sortc-"


@pytest.fixture(autouse=True)
def _clean():
    def wipe():
        with unit_of_work() as session:
            session.query(Finding).filter(Finding.finding_id.like(f"{PREFIX}%")).delete(
                synchronize_session=False
            )

    wipe()
    yield
    wipe()


def _create(finding_id: str, timestamp: datetime, created_at: datetime):
    DatabaseService().create_finding(
        finding_id=finding_id,
        mitre_predictions={},
        anomaly_score=0.5,
        timestamp=timestamp,
        data_source="test",
        severity="low",
        status="new",
    )
    with unit_of_work() as session:
        row = session.get(Finding, finding_id)
        assert row is not None
        row.created_at = created_at


def _ids(**kwargs):
    return [
        finding.finding_id
        for finding in DatabaseService().get_findings(
            search_query=PREFIX, limit=10, **kwargs
        )
        if finding.finding_id.startswith(PREFIX)
    ]


def test_sort_by_created_at_orders_by_arrival_not_event_time():
    # event timestamps run opposite to arrival, so a timestamp sort cannot pass
    _create(
        f"{PREFIX}arrived-first",
        timestamp=datetime(2026, 6, 2),
        created_at=datetime(2026, 1, 1),
    )
    _create(
        f"{PREFIX}arrived-second",
        timestamp=datetime(2026, 6, 1),
        created_at=datetime(2026, 1, 2),
    )

    assert _ids(sort_by="created_at", sort_order="desc") == [
        f"{PREFIX}arrived-second",
        f"{PREFIX}arrived-first",
    ]
    assert _ids(sort_by="created_at", sort_order="asc") == [
        f"{PREFIX}arrived-first",
        f"{PREFIX}arrived-second",
    ]
    by_timestamp = _ids(sort_by="timestamp", sort_order="desc")
    assert by_timestamp == [
        f"{PREFIX}arrived-first",
        f"{PREFIX}arrived-second",
    ]
    assert _ids(sort_by="not-a-column", sort_order="desc") == by_timestamp
