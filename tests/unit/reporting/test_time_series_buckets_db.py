"""The trend buckets split the whole window, so "all" reaches today (#3003)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.reporting.analytics_service import get_time_series_data
from core.storage.models import Finding
from core.storage.unit_of_work import unit_of_work
from core.threat_intel.mitre_lookup import get_time_range
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


@pytest.fixture(autouse=True)
def _clean(throwaway_database):
    def purge():
        with unit_of_work() as session:
            session.query(Finding).filter(Finding.finding_id.like("tsb-%")).delete(
                synchronize_session=False
            )

    purge()
    yield
    purge()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "time_range, count, width",
    [
        ("24h", 24, timedelta(hours=1)),
        ("7d", 28, timedelta(hours=6)),
        ("30d", 30, timedelta(days=1)),
        ("all", 30, None),
    ],
)
async def test_buckets_cover_the_window_and_count_a_recent_finding(
    time_range, count, width
):
    created = utcnow() - timedelta(minutes=30)
    with unit_of_work() as session:
        session.add(
            Finding(
                finding_id=f"tsb-{time_range}",
                timestamp=created,
                created_at=created,
                data_source="splunk",
                severity="high",
                entity_context={"hostname": "tsb-host"},
            )
        )

    start, end = get_time_range(time_range)
    with unit_of_work() as session:
        series = await get_time_series_data(session, start, end, time_range)

    starts = [datetime.fromisoformat(b["timestamp"]) for b in series]
    assert len(series) == count
    assert starts[0] == start
    if width is not None:
        assert starts == [start + width * i for i in range(count)]
    assert abs(end - starts[-1] - (end - start) / count) < timedelta(seconds=1)
    assert series[-1]["findings"] >= 1
    assert series[-1]["alerts"] >= 1
