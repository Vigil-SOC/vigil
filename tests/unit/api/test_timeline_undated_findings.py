"""Timeline routes over findings that have no timestamp.

``findings.timestamp`` is nullable (``25_nullable_finding_score_timestamp.sql``):
LogLM parquet ingest leaves it NULL when a row carries no ``event_start_time``.
The routes parsed every finding's time unconditionally, so a single undated
finding turned the whole response into a 500.

DB-backed because the NULL ordering is half of it: ``ORDER BY timestamp DESC``
puts NULLs first in Postgres, so a page of findings can be nothing but undated
rows, and a route that skips them after fetching that page shows nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.storage.models import Case, Finding
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

# Naive UTC, as ingest stores it, and far from any other test's utcnow() so no
# other module's findings land in a window around it.
T0 = datetime(2001, 9, 9, 1, 46, 40)

# The context and cluster routes read one get_findings(limit=1000) page.
PAGE = 1000


# Asks for throwaway_database outright rather than trusting autouse order, so
# the purge below can only ever run against the database built for this run.
@pytest.fixture(autouse=True)
def _clean(throwaway_database):
    def purge():
        with unit_of_work() as session:
            session.query(Case).filter(Case.case_id.like("tl-%")).delete(
                synchronize_session=False
            )
            session.query(Finding).filter(Finding.finding_id.like("tl-%")).delete(
                synchronize_session=False
            )

    # After as well as before: the undated page would otherwise sit at the head
    # of every later test's timestamp-ordered read.
    purge()
    yield
    purge()


@pytest.fixture
def client():
    from services.api.routers import timeline

    app = FastAPI()
    app.include_router(timeline.router, prefix=timeline.ROUTER_META.prefix)
    # A route that raises should read as the 500 the console got.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def undated_page():
    """A full page of undated findings, which ``timestamp DESC`` returns first."""
    _seed(*(_finding(f"tl-crowd-{i:04d}", None) for i in range(PAGE)))


def _finding(finding_id: str, at: Optional[datetime], **kw) -> Finding:
    return Finding(
        finding_id=finding_id,
        timestamp=at,
        data_source="loglm",
        severity="high",
        **kw,
    )


def _seed(*rows) -> None:
    with unit_of_work() as session:
        session.add_all(rows)


def _finding_ids(body: dict) -> list:
    return [
        e["metadata"]["finding_id"] for e in body["events"] if e["type"] == "finding"
    ]


def test_case_timeline_leaves_out_an_undated_finding(client):
    _seed(
        Case(
            case_id="tl-case",
            title="LogLM import",
            findings=[_finding("tl-dated", T0), _finding("tl-undated", None)],
        )
    )

    r = client.get("/api/timeline/case/tl-case")

    assert r.status_code == 200, r.text
    body = r.json()
    assert _finding_ids(body) == ["tl-dated"]
    assert [e["type"] for e in body["events"]] == ["finding", "status"]
    assert body["total"] == 2


def test_the_context_of_an_undated_finding_is_409(client):
    _seed(_finding("tl-undated", None))

    r = client.get("/api/timeline/finding/tl-undated/context")

    assert r.status_code == 409
    assert r.json()["detail"] == (
        "Finding tl-undated has no timestamp, so there is no time window around it"
    )


def test_an_unknown_finding_is_still_404(client):
    r = client.get("/api/timeline/finding/tl-missing/context")

    assert r.status_code == 404


def test_the_context_window_leaves_out_undated_neighbours(client):
    _seed(
        _finding("tl-target", T0),
        _finding("tl-near", T0 + timedelta(minutes=5)),
        _finding("tl-far", T0 + timedelta(hours=3)),
        _finding("tl-undated", None),
    )

    r = client.get(
        "/api/timeline/finding/tl-target/context", params={"time_window_minutes": 60}
    )

    assert r.status_code == 200, r.text
    events = r.json()["events"]
    assert [e["metadata"]["finding_id"] for e in events] == ["tl-target", "tl-near"]
    assert [e["metadata"]["is_target"] for e in events] == [True, False]


def test_a_page_of_undated_findings_does_not_empty_the_context(client, undated_page):
    _seed(_finding("tl-target", T0), _finding("tl-near", T0 + timedelta(minutes=5)))

    r = client.get("/api/timeline/finding/tl-target/context")

    assert r.status_code == 200, r.text
    assert _finding_ids(r.json()) == ["tl-target", "tl-near"]


def test_cluster_timeline_leaves_out_undated_findings(client):
    _seed(
        _finding("tl-dated", T0, cluster_id="tl-cluster"),
        _finding("tl-undated", None, cluster_id="tl-cluster"),
    )

    r = client.get("/api/timeline/cluster/tl-cluster")

    assert r.status_code == 200, r.text
    body = r.json()
    assert _finding_ids(body) == ["tl-dated"]
    assert body["total"] == 1


def test_a_cluster_of_only_undated_findings_is_empty_not_missing(client):
    _seed(_finding("tl-undated", None, cluster_id="tl-cluster"))

    r = client.get("/api/timeline/cluster/tl-cluster")

    assert r.status_code == 200, r.text
    assert r.json() == {"events": [], "total": 0, "start_time": None, "end_time": None}


def test_an_unknown_cluster_is_still_404(client):
    r = client.get("/api/timeline/cluster/tl-no-such-cluster")

    assert r.status_code == 404


def test_a_page_of_undated_findings_does_not_hide_a_cluster(client, undated_page):
    _seed(_finding("tl-dated", T0, cluster_id="tl-cluster"))

    r = client.get("/api/timeline/cluster/tl-cluster")

    assert r.status_code == 200, r.text
    assert _finding_ids(r.json()) == ["tl-dated"]


def test_a_clusters_own_undated_findings_do_not_hide_its_dated_ones(client):
    # LogLM parquet ingest sets cluster_id from attack_id on the same rows that
    # can arrive without an event time.
    _seed(
        *(
            _finding(f"tl-crowd-{i:04d}", None, cluster_id="tl-cluster")
            for i in range(PAGE)
        ),
        _finding("tl-dated", T0, cluster_id="tl-cluster"),
    )

    r = client.get("/api/timeline/cluster/tl-cluster")

    assert r.status_code == 200, r.text
    assert _finding_ids(r.json()) == ["tl-dated"]
