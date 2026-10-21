"""The command bar's by-ioc search finds a case through its alerts' entities (#2563).

Runs on the throwaway database tests/unit/conftest.py provisions per process.
"""

import pytest
from sqlalchemy import insert

from core.cases.case_search_service import CaseSearchService
from core.storage.models import Case, CaseIOC, Finding, case_findings
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

PREFIX = "csbe-"


@pytest.fixture(autouse=True)
def _seed():
    now = utcnow()
    with unit_of_work() as session:
        session.execute(
            insert(Finding),
            [
                {
                    "finding_id": f"{PREFIX}f-host",
                    "data_source": "zeek",
                    "severity": "high",
                    "timestamp": now,
                    "entity_context": {
                        "hostnames": ["FIN-WS-114"],
                        "usernames": ["jdoe"],
                    },
                },
                {
                    "finding_id": f"{PREFIX}f-other",
                    "data_source": "zeek",
                    "severity": "low",
                    "timestamp": now,
                    "entity_context": {"hostnames": ["HR-LAP-02"]},
                },
            ],
        )
        for case_id in ("host", "other", "both"):
            session.add(
                Case(
                    case_id=f"{PREFIX}{case_id}",
                    title=case_id,
                    status="open",
                    priority="high",
                )
            )
        session.flush()
        session.execute(
            insert(case_findings),
            [
                {"case_id": f"{PREFIX}host", "finding_id": f"{PREFIX}f-host"},
                {"case_id": f"{PREFIX}other", "finding_id": f"{PREFIX}f-other"},
                {"case_id": f"{PREFIX}both", "finding_id": f"{PREFIX}f-host"},
            ],
        )
        session.add(
            CaseIOC(case_id=f"{PREFIX}both", ioc_type="hostname", value="FIN-WS-114")
        )
    yield
    with unit_of_work() as session:
        session.query(Case).filter(Case.case_id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )
        session.query(Finding).filter(Finding.finding_id.like(f"{PREFIX}%")).delete(
            synchronize_session=False
        )


def _search(term, **kwargs):
    rows = CaseSearchService().search_by_ioc(term, **kwargs)
    return {row["case_id"]: row.get("match_type") for row in rows}


@pytest.mark.parametrize("term", ["fin-ws-114", "WS-114"])
def test_case_found_through_its_alerts_entities(term):
    found = _search(term)
    # each case once; only the one with no CaseIOC row is marked as an alert match
    assert found == {f"{PREFIX}host": "alert", f"{PREFIX}both": None}


def test_user_matches_only_through_alerts():
    assert _search("JDOE") == {f"{PREFIX}host": "alert", f"{PREFIX}both": "alert"}


def test_ioc_type_keeps_the_ioc_only_match():
    assert _search("fin-ws-114", ioc_type="hostname") == {f"{PREFIX}both": None}
