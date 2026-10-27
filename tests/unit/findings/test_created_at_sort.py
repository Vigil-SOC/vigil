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
    )
    with unit_of_work() as session:
        row = session.get(Finding, finding_id)
        assert row is not None
        row.created_at = created_at


def _scored(finding_id, score, timestamp):
    DatabaseService().create_finding(
        finding_id=finding_id,
        mitre_predictions={},
        anomaly_score=score,
        timestamp=timestamp,
        data_source="test",
        severity="low",
    )


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


def test_null_anomaly_score_sorts_last_in_both_directions():
    _scored(f"{PREFIX}score-high", 0.9, datetime(2026, 1, 1))
    _scored(f"{PREFIX}score-low", 0.2, datetime(2026, 1, 2))
    _scored(f"{PREFIX}score-none", None, datetime(2026, 1, 3))

    assert _ids(sort_by="anomaly_score", sort_order="desc") == [
        f"{PREFIX}score-high",
        f"{PREFIX}score-low",
        f"{PREFIX}score-none",
    ]
    assert _ids(sort_by="anomaly_score", sort_order="asc") == [
        f"{PREFIX}score-low",
        f"{PREFIX}score-high",
        f"{PREFIX}score-none",
    ]


def test_equal_scores_sort_newest_timestamp_first_on_every_call():
    _scored(f"{PREFIX}eq-older", 0.5, datetime(2026, 6, 1))
    _scored(f"{PREFIX}eq-newer", 0.5, datetime(2026, 6, 2))

    expected = [f"{PREFIX}eq-newer", f"{PREFIX}eq-older"]
    assert _ids(sort_by="anomaly_score", sort_order="desc") == expected
    assert _ids(sort_by="anomaly_score", sort_order="desc") == expected
    assert _ids(sort_by="anomaly_score", sort_order="asc") == expected


def test_null_timestamp_sorts_last_in_both_directions():
    _scored(f"{PREFIX}dated", 0.1, datetime(2026, 6, 1))
    _scored(f"{PREFIX}undated", 0.9, None)

    expected = [f"{PREFIX}dated", f"{PREFIX}undated"]
    assert _ids(sort_by="timestamp", sort_order="desc") == expected
    assert _ids(sort_by="timestamp", sort_order="asc") == expected


def test_shared_score_and_timestamp_tiebreak_on_finding_id():
    when = datetime(2026, 6, 1, 12, 0, 0)
    _scored(f"{PREFIX}tie-b", 0.4, when)
    _scored(f"{PREFIX}tie-a", 0.4, when)

    expected = [f"{PREFIX}tie-a", f"{PREFIX}tie-b"]
    assert _ids(sort_by="anomaly_score", sort_order="desc") == expected
    assert _ids(sort_by="anomaly_score", sort_order="desc") == expected
