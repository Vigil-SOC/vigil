"""Case and finding ``/stats/summary`` count in SQL, not over a loaded page (#1438).

Both summaries used to load rows through ``get_cases()`` / ``get_findings()``,
whose default limit is 10,000, and count them in Python. Past that many rows
``total`` read 10000 while the list endpoints reported the real number.

Runs on the throwaway database tests/unit/conftest.py provisions per process.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from core.findings import exclusions as ex
from core.storage.models import Case, Finding, IpExclusion
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

PREFIX = "sss-"
SCANNER = "203.0.113.19"
# Past the old 10,000-row cap, so a capped summary would read 10000.
OVER_CAP = 10_050


def _delete_test_rows():
    with unit_of_work() as session:
        session.query(IpExclusion).filter(IpExclusion.ip == SCANNER).delete(
            synchronize_session=False
        )
        session.query(Finding).filter(Finding.finding_id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
        session.query(Case).filter(Case.case_id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )


@pytest.fixture(autouse=True)
def _clean():
    ex.invalidate_cache()
    _delete_test_rows()
    yield
    _delete_test_rows()
    ex.invalidate_cache()


@pytest.fixture
def client(authenticate_app):
    from core.api.v1.cases_router import router as cases_router
    from core.api.v1.findings_router import router as findings_router

    app = FastAPI()
    app.include_router(findings_router, prefix="/api/v1/findings")
    app.include_router(cases_router, prefix="/api/v1/cases")
    authenticate_app(app)
    return TestClient(app)


def _seed_findings(rows):
    now = utcnow()
    with unit_of_work() as session:
        session.execute(
            insert(Finding),
            [
                {
                    "finding_id": f"{PREFIX}{i}",
                    "data_source": "loglm",
                    "status": "new",
                    "timestamp": now,
                    **row,
                }
                for i, row in enumerate(rows)
            ],
        )


def _seed_cases(rows):
    with unit_of_work() as session:
        session.execute(
            insert(Case),
            [
                {"case_id": f"{PREFIX}{i}", "title": f"case {i}", **row}
                for i, row in enumerate(rows)
            ],
        )


def _seeded(id_column, criterion):
    with unit_of_work() as session:
        return session.execute(
            select(func.count()).where(id_column.like(f"{PREFIX}%"), criterion)
        ).scalar_one()


def _findings_total(client, view="include"):
    response = client.get("/api/v1/findings", params={"exclusions": view, "limit": 1})
    assert response.status_code == 200, response.text
    return response.json()["total"]


def _findings_summary(client, view="include"):
    response = client.get("/api/v1/findings/stats/summary", params={"exclusions": view})
    assert response.status_code == 200, response.text
    return response.json()


def _cases_summary(client):
    response = client.get("/api/v1/cases/stats/summary")
    assert response.status_code == 200, response.text
    return response.json()


def _cases_list_total(client):
    """Open plus closed: the case list defaults to open cases only."""
    total = 0
    for closed in ("false", "true"):
        response = client.get("/api/v1/cases", params={"closed": closed, "limit": 1})
        assert response.status_code == 200, response.text
        total += response.json()["total"]
    return total


def test_findings_summary_breakdowns_and_unknown_bucket(client):
    before = _findings_summary(client)
    _seed_findings(
        [
            {"severity": "high", "data_source": "splunk"},
            {"severity": "high", "data_source": "splunk"},
            {"severity": "low", "data_source": "elastic"},
            {"severity": None, "data_source": "loglm"},
            {"severity": "", "data_source": ""},
        ]
    )
    after = _findings_summary(client)

    def delta(field, key):
        return after[field].get(key, 0) - before[field].get(key, 0)

    assert after["total"] - before["total"] == 5
    assert delta("by_severity", "high") == 2
    assert delta("by_severity", "low") == 1
    # A null and an empty severity both count as "unknown", as they did before.
    assert delta("by_severity", "unknown") == 2
    assert "" not in after["by_severity"]
    assert delta("by_data_source", "splunk") == 2
    assert delta("by_data_source", "elastic") == 1
    assert delta("by_data_source", "unknown") == 1
    assert sum(after["by_severity"].values()) == after["total"]
    assert sum(after["by_data_source"].values()) == after["total"]
    assert after["total"] == _findings_total(client)


def test_findings_summary_honours_exclusions_like_the_list(client):
    _seed_findings(
        [
            {"severity": "high", "entity_context": {"src_ip": SCANNER}},
            {"severity": "high", "entity_context": {"dest_ips": [SCANNER]}},
            {"severity": "low", "entity_context": {"src_ip": "192.0.2.80"}},
        ]
    )
    with unit_of_work() as session:
        ex.create_exclusion(
            session, ip=SCANNER, reason="known scanner", created_by="analyst-1"
        )

    summaries = {view: _findings_summary(client, view) for view in ex.EXCLUSION_VIEWS}
    for view, summary in summaries.items():
        assert summary["total"] == _findings_total(client, view), view
        assert sum(summary["by_severity"].values()) == summary["total"], view
    assert summaries["only"]["total"] == 2
    assert summaries["only"]["by_severity"] == {"high": 2}
    assert (
        summaries["hide"]["total"] + summaries["only"]["total"]
        == summaries["include"]["total"]
    )


def test_findings_summary_is_not_capped_at_ten_thousand(client):
    _seed_findings(
        [{"severity": "medium" if i % 2 else "high"} for i in range(OVER_CAP)]
    )
    summary = _findings_summary(client)

    assert summary["total"] >= OVER_CAP
    assert summary["total"] == _findings_total(client)
    seeded_medium = _seeded(Finding.finding_id, Finding.severity == "medium")
    assert seeded_medium == OVER_CAP // 2
    assert summary["by_severity"]["medium"] >= seeded_medium


def test_cases_summary_breakdowns(client):
    before = _cases_summary(client)
    _seed_cases(
        [
            {"status": "new", "priority": "high"},
            {"status": "new", "priority": "medium"},
            {"status": "in_progress", "priority": "high"},
            {"status": "closed", "priority": "low"},
        ]
    )
    after = _cases_summary(client)

    def delta(field, key):
        return after[field].get(key, 0) - before[field].get(key, 0)

    assert after["total"] - before["total"] == 4
    assert delta("by_status", "new") == 2
    assert delta("by_status", "in_progress") == 1
    assert delta("by_status", "closed") == 1
    assert delta("by_priority", "high") == 2
    assert delta("by_priority", "medium") == 1
    assert delta("by_priority", "low") == 1
    assert sum(after["by_status"].values()) == after["total"]
    assert sum(after["by_priority"].values()) == after["total"]
    assert after["total"] == _cases_list_total(client)


def test_cases_summary_is_not_capped_at_ten_thousand(client):
    _seed_cases(
        [
            {"status": "closed" if i % 5 == 0 else "new", "priority": "medium"}
            for i in range(OVER_CAP)
        ]
    )
    summary = _cases_summary(client)

    assert summary["total"] >= OVER_CAP
    assert summary["total"] == _cases_list_total(client)
    seeded_closed = _seeded(Case.case_id, Case.status == "closed")
    assert seeded_closed == len(range(0, OVER_CAP, 5))
    assert summary["by_status"]["closed"] >= seeded_closed
    assert sum(summary["by_status"].values()) == summary["total"]
