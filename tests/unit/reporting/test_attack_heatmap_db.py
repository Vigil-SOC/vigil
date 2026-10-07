"""Attack time heatmap over findings that have no timestamp.

``findings.timestamp`` is nullable (LogLM parquet ingest leaves it NULL when a
row carries no ``event_start_time``). The heatmap loaded every finding in the
window and called ``timestamp.weekday()`` on each, so one undated finding
turned the whole ``/api/analytics`` response into a 500.

DB-backed because the binning is done by Postgres: the day index has to come
out the way Python's ``weekday()`` numbers it (Monday=0 .. Sunday=6), not the
way ``extract(dow ...)`` does (Sunday=0).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.reporting.analytics_service import get_attack_time_heatmap
from core.storage.models import Finding
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

# Ingest time for the service-level tests: far from any other test's utcnow()
# so no other module's findings land in the window.
CREATED = datetime(2001, 9, 1, 12, 0, 0)

# Event times. 2001-09-09 is a Sunday: weekday() 6, isodow 7, dow 0.
SUNDAY_23 = datetime(2001, 9, 9, 23, 15)
MONDAY_13 = datetime(2001, 9, 10, 13, 5)
WEDNESDAY_0 = datetime(2001, 9, 12, 0, 30)


@pytest.fixture(autouse=True)
def _clean(throwaway_database):
    def purge():
        with unit_of_work() as session:
            session.query(Finding).filter(Finding.finding_id.like("hm-%")).delete(
                synchronize_session=False
            )

    purge()
    yield
    purge()


@pytest.fixture
def client():
    from services.api.routers import analytics

    app = FastAPI()
    app.include_router(analytics.router, prefix=analytics.ROUTER_META.prefix)
    # A route that raises should read as the 500 the console got.
    return TestClient(app, raise_server_exceptions=False)


def _finding(
    finding_id: str,
    at: Optional[datetime],
    severity: str = "low",
    created_at: datetime = CREATED,
) -> Finding:
    return Finding(
        finding_id=finding_id,
        timestamp=at,
        created_at=created_at,
        data_source="loglm",
        severity=severity,
    )


def _seed(*rows) -> None:
    with unit_of_work() as session:
        session.add_all(rows)


def _cell(grid: list, day: int, hour: int) -> dict:
    (cell,) = [c for c in grid if c["dayNum"] == day and c["hour"] == hour]
    return cell


def test_analytics_is_200_with_an_undated_finding(client):
    now = datetime.utcnow()
    _seed(
        _finding("hm-dated", SUNDAY_23, "critical", created_at=now),
        _finding("hm-undated", None, "critical", created_at=now),
    )

    r = client.get("/api/analytics", params={"time_range": "24h"})

    assert r.status_code == 200, r.text
    grid = r.json()["attackHeatmap"]
    assert len(grid) == 7 * 24


@pytest.mark.asyncio
async def test_undated_findings_are_left_out_and_dated_ones_binned():
    _seed(
        _finding("hm-sun-crit", SUNDAY_23, "critical"),
        _finding("hm-mon-crit", MONDAY_13, "critical"),
        _finding("hm-mon-high", MONDAY_13 + timedelta(minutes=40), "high"),
        _finding("hm-mon-low", MONDAY_13, "low"),
        _finding("hm-wed-med", WEDNESDAY_0, "medium"),
        _finding("hm-undated-crit", None, "critical"),
        _finding("hm-undated-high", None, "high"),
        # Outside the created_at window: not counted even though dated.
        _finding(
            "hm-late", MONDAY_13, "critical", created_at=CREATED + timedelta(days=30)
        ),
    )

    with unit_of_work() as session:
        grid = await get_attack_time_heatmap(
            session, CREATED - timedelta(hours=1), CREATED + timedelta(hours=1)
        )

    # Shape is unchanged: a full 7 x 24 grid in day-major order.
    assert len(grid) == 7 * 24
    assert [(c["dayNum"], c["hour"]) for c in grid] == [
        (d, h) for d in range(7) for h in range(24)
    ]
    assert set(grid[0]) == {
        "day",
        "dayNum",
        "hour",
        "count",
        "critical",
        "high",
        "intensity",
    }

    sunday = _cell(grid, SUNDAY_23.weekday(), 23)
    assert (sunday["day"], sunday["dayNum"]) == ("Sunday", 6)
    assert (sunday["count"], sunday["critical"], sunday["high"]) == (1, 1, 0)

    monday = _cell(grid, MONDAY_13.weekday(), 13)
    assert (monday["day"], monday["dayNum"]) == ("Monday", 0)
    assert (monday["count"], monday["critical"], monday["high"]) == (3, 1, 1)
    assert monday["intensity"] == 3

    wednesday = _cell(grid, WEDNESDAY_0.weekday(), 0)
    assert (wednesday["count"], wednesday["critical"], wednesday["high"]) == (1, 0, 0)

    # The two undated findings land nowhere; the out-of-window one neither.
    assert sum(c["count"] for c in grid) == 5
    assert sum(c["critical"] for c in grid) == 2
    assert sum(c["high"] for c in grid) == 1
