"""Analytics breakdowns leave known-answer probes out, the way the totals do.

Probes (#923) are findings with ``data_source = "probe"``: the daemon testing
itself. #1027 took them out of the headline total and the trend buckets, but the
severity distribution, affected entities and attack heatmap still counted them,
so a breakdown of the findings total did not add up to it.

The per-source breakdown keeps them on purpose (#1027), as their own row.

DB-backed because the filter is a SQL predicate.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.reporting.analytics_service import (
    calculate_metrics,
    get_affected_entities,
    get_attack_time_heatmap,
    get_severity_distribution,
    get_top_alert_sources,
)
from core.storage.models import Finding
from core.storage.unit_of_work import unit_of_work

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

# Far from any other test's utcnow() and from test_attack_heatmap_db's window,
# so no other module's findings land in it.
CREATED = datetime(2002, 3, 1, 12, 0, 0)
START = CREATED - timedelta(hours=1)
END = CREATED + timedelta(hours=1)
# 2002-03-04 is a Monday.
EVENT = datetime(2002, 3, 4, 9, 30)


@pytest.fixture(autouse=True)
def _clean(throwaway_database):
    def purge():
        with unit_of_work() as session:
            session.query(Finding).filter(Finding.finding_id.like("px-%")).delete(
                synchronize_session=False
            )

    purge()
    yield
    purge()


def _finding(finding_id: str, data_source: str, severity: str, host: str) -> Finding:
    return Finding(
        finding_id=finding_id,
        timestamp=EVENT,
        created_at=CREATED,
        data_source=data_source,
        severity=severity,
        entity_context={"hostname": host},
    )


@pytest.fixture
def seeded():
    with unit_of_work() as session:
        session.add_all(
            [
                _finding("px-real-1", "splunk", "critical", "web-01"),
                _finding("px-real-2", "splunk", "high", "web-01"),
                _finding("px-real-3", "crowdstrike", "low", "db-01"),
                # Probes: same window, same event time, a host of their own and
                # a severity no real finding has.
                _finding("px-probe-1", "probe", "critical", "probe-host"),
                _finding("px-probe-2", "probe", "medium", "probe-host"),
            ]
        )
    return 3  # real findings


@pytest.mark.asyncio
async def test_breakdowns_exclude_probes_and_add_up_to_the_total(seeded):
    with unit_of_work() as session:
        metrics = await calculate_metrics(
            session, START, END, START - timedelta(hours=2), START
        )
        severity = await get_severity_distribution(session, START, END)
        entities = await get_affected_entities(session, START, END)
        heatmap = await get_attack_time_heatmap(session, START, END)

    assert metrics["totalFindings"] == seeded

    # Severity: no probe-only Medium slice, critical counts the real one only.
    by_severity = {row["name"]: row["value"] for row in severity}
    assert by_severity == {"Critical": 1, "High": 1, "Low": 1}
    assert sum(by_severity.values()) == metrics["totalFindings"]

    # Entities: the probe host is not an affected entity.
    by_entity = {row["entity"]: row for row in entities}
    assert set(by_entity) == {"web-01", "db-01"}
    assert by_entity["web-01"]["critical"] == 1
    assert sum(row["count"] for row in entities) == metrics["totalFindings"]

    # Heatmap: shape unchanged, one cell holds exactly the real findings.
    assert len(heatmap) == 7 * 24
    (cell,) = [c for c in heatmap if c["count"]]
    assert (cell["dayNum"], cell["hour"]) == (EVENT.weekday(), EVENT.hour)
    assert (cell["count"], cell["critical"], cell["high"]) == (3, 1, 1)
    assert sum(c["count"] for c in heatmap) == metrics["totalFindings"]


@pytest.mark.asyncio
async def test_top_sources_keep_probes_as_their_own_row(seeded):
    with unit_of_work() as session:
        sources = await get_top_alert_sources(session, START, END)

    by_source = {row["name"]: row["count"] for row in sources}
    assert by_source == {"splunk": 2, "probe": 2, "crowdstrike": 1}
    # Without the probe row, the sources add up to the total.
    assert sum(n for name, n in by_source.items() if name != "probe") == seeded
