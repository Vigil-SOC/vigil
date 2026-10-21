"""Findings search matches every whitespace/comma-separated term (#2314).

Runs on the throwaway database tests/unit/conftest.py provisions per process.
"""

from datetime import datetime

import pytest

from core.storage.models import Finding
from core.storage.service import DatabaseService
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

PREFIX = "srchterm-"
A, B = "10.91.0.1", "172.91.0.2"


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


def _create(suffix: str, description: str, entity_context=None):
    DatabaseService().create_finding(
        finding_id=f"{PREFIX}{suffix}",
        mitre_predictions={},
        anomaly_score=0.5,
        timestamp=datetime(2026, 6, 1),
        data_source="test",
        severity="low",
        description=description,
        entity_context=entity_context,
    )


@pytest.mark.parametrize(
    "query,expected",
    [
        (f"{A} {B}", {"both-desc", "both-split"}),
        (f"{A}, {B}", {"both-desc", "both-split"}),
        (f"  {A},,{B}  ", {"both-desc", "both-split"}),
        (A, {"both-desc", "both-split", "only-a"}),
    ],
)
def test_every_term_must_match_and_count_agrees(query, expected):
    _create("both-desc", f"Beaconing {A} ↔ {B}")
    # terms may match different columns
    _create("both-split", f"Scan from {A}", entity_context={"dst_ips": [B]})
    _create("only-a", f"Scan from {A}")

    service = DatabaseService()
    rows = service.get_findings(search_query=query, limit=100)
    ids = {f.finding_id.removeprefix(PREFIX) for f in rows}
    assert ids == expected
    assert service.count_findings(search_query=query) == len(rows)


def test_blank_query_is_no_search():
    _create("only-a", f"Scan from {A}")
    service = DatabaseService()
    assert service.count_findings(search_query=" , ") == service.count_findings()
