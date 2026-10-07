"""``/api/timeline/range`` filters in the query, before the limit.

The route fetched the newest ``limit`` findings and applied the window, the
severity and the data source to that page afterwards. A window older than the
newest ``limit`` findings came back empty, and a filter matching only older
findings came back short.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.storage.models import Finding
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

# Naive UTC, as ingest stores it, and far from any other test's utcnow() so no
# other module's findings land in the windows below.
T0 = datetime(2002, 3, 4, 5, 6, 7)

LIMIT = 5


@pytest.fixture(autouse=True)
def _clean(throwaway_database):
    def purge():
        with unit_of_work() as session:
            session.query(Finding).filter(Finding.finding_id.like("tlr-%")).delete(
                synchronize_session=False
            )

    purge()
    yield
    purge()


@pytest.fixture
def client():
    from services.api.routers import timeline

    app = FastAPI()
    app.include_router(timeline.router, prefix=timeline.ROUTER_META.prefix)
    return TestClient(app, raise_server_exceptions=False)


def _finding(finding_id: str, at: datetime, **kw) -> Finding:
    kw.setdefault("data_source", "splunk")
    kw.setdefault("severity", "low")
    return Finding(finding_id=finding_id, timestamp=at, **kw)


def _seed(*rows) -> None:
    with unit_of_work() as session:
        session.add_all(rows)


@pytest.fixture
def recent_crowd():
    """More than ``LIMIT`` findings newer than any window the tests ask for."""
    _seed(
        *(
            _finding(f"tlr-recent-{i:02d}", T0 + timedelta(days=30, minutes=i))
            for i in range(LIMIT * 2)
        )
    )


def _ids(r) -> list:
    assert r.status_code == 200, r.text
    return [e["metadata"]["finding_id"] for e in r.json()["events"]]


def _window(**params) -> dict:
    return {
        "start": T0.isoformat() + "Z",
        "end": (T0 + timedelta(hours=1)).isoformat() + "Z",
        "limit": LIMIT,
        **params,
    }


def test_an_older_window_returns_its_findings(client, recent_crowd):
    _seed(
        _finding("tlr-old-a", T0 + timedelta(minutes=1)),
        _finding("tlr-old-b", T0 + timedelta(minutes=2)),
        _finding("tlr-before", T0 - timedelta(minutes=1)),
    )

    r = client.get("/api/timeline/range", params=_window())

    assert _ids(r) == ["tlr-old-a", "tlr-old-b"]
    assert r.json()["total"] == 2


def test_the_limit_counts_findings_inside_the_window(client, recent_crowd):
    _seed(
        *(
            _finding(f"tlr-old-{i:02d}", T0 + timedelta(minutes=i))
            for i in range(LIMIT + 2)
        )
    )

    r = client.get("/api/timeline/range", params=_window())

    # The newest LIMIT findings in the window, oldest first.
    assert _ids(r) == [f"tlr-old-{i:02d}" for i in range(2, LIMIT + 2)]


def test_severity_is_filtered_before_the_limit(client):
    _seed(
        *(
            _finding(f"tlr-low-{i:02d}", T0 + timedelta(minutes=30 + i))
            for i in range(LIMIT * 2)
        ),
        _finding("tlr-crit", T0 + timedelta(minutes=1), severity="critical"),
    )

    r = client.get("/api/timeline/range", params=_window(severity="critical"))

    assert _ids(r) == ["tlr-crit"]


def test_data_source_is_filtered_before_the_limit(client):
    _seed(
        *(
            _finding(f"tlr-splunk-{i:02d}", T0 + timedelta(minutes=30 + i))
            for i in range(LIMIT * 2)
        ),
        _finding("tlr-loglm", T0 + timedelta(minutes=1), data_source="loglm"),
    )

    r = client.get("/api/timeline/range", params=_window(data_source="loglm"))

    assert _ids(r) == ["tlr-loglm"]
