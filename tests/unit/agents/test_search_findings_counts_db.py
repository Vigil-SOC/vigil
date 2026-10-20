"""search_findings counts every match, in SQL, ahead of the page (#2312).

Runs on the throwaway database tests/unit/conftest.py provisions per process.
"""

import json

import pytest
from sqlalchemy import insert

from core.agents import tool_registry
from core.agents.tool_registry import execute_backend_tool
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import Finding
from core.storage.unit_of_work import unit_of_work
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

PREFIX = "sfc-"
HOST = "10.11.11.5"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    def delete():
        with unit_of_work() as session:
            session.query(Finding).filter(Finding.finding_id.like(f"{PREFIX}%")).delete(
                synchronize_session=False
            )

    monkeypatch.setattr(tool_registry, "_data", DatabaseDataService)
    delete()
    yield
    delete()


def _seed():
    """23 findings naming HOST under every key shape, one that does not."""
    severities = ["critical"] * 3 + ["high"] * 8 + ["medium"] * 12
    contexts = [
        {"src_ip": HOST, "dst_ip": "192.0.2.1"},
        {"src_ips": [HOST, HOST], "dest_ips": ["192.0.2.1", "192.0.2.2"]},
        {"source_ips": [HOST], "src_ip": HOST, "dst_ips": "192.0.2.3"},
    ]
    rows = [
        {
            "finding_id": f"{PREFIX}{i}",
            "severity": severity,
            "status": "new" if i % 2 else "triaged",
            "data_source": "zeek" if i < 10 else "darktrace",
            "entity_context": contexts[i % 3],
        }
        for i, severity in enumerate(severities)
    ]
    rows += [
        {"finding_id": f"{PREFIX}{i}-x", "severity": "low", "entity_context": c}
        for i, c in enumerate([{"src_ip": f"198.51.100.{n}"} for n in range(12)])
    ]
    with unit_of_work() as session:
        session.execute(
            insert(Finding),
            [
                {"data_source": "zeek", "status": "new", "timestamp": utcnow(), **row}
                for row in rows
            ],
        )


@pytest.mark.asyncio
async def test_counts_cover_every_match_not_the_page():
    _seed()

    result, handled = await execute_backend_tool(
        "search_findings", {"query": HOST, "limit": 5}
    )

    assert handled is True
    assert len(result["findings"]) == 5
    assert result["total"] == 23
    assert result["by_severity"] == {"medium": 12, "high": 8, "critical": 3}
    assert result["by_data_source"] == {"darktrace": 13, "zeek": 10}
    assert result["by_state"] == {"open": 23}
    assert {f["state"] for f in result["findings"]} == {"waiting"}
    # Each finding counts once per address, whichever key or how often it names it.
    assert result["by_source_ip"] == {HOST: 23}
    assert result["by_destination_ip"] == {
        "192.0.2.1": 16,
        "192.0.2.2": 8,
        "192.0.2.3": 7,
    }
    # The counts come first, so a cap that cuts the tail cuts the page.
    keys = list(result)
    assert keys.index("by_destination_ip") < keys.index("findings") == len(keys) - 1
    head = json.dumps(result)[: json.dumps(result).index('"findings"')]
    assert '"by_severity"' in head


@pytest.mark.asyncio
async def test_ip_facets_keep_the_busiest_and_count_the_rest():
    _seed()

    result, _ = await execute_backend_tool(
        "search_findings", {"query": PREFIX, "limit": 1}
    )

    assert result["total"] == 35
    assert len(result["by_source_ip"]) == tool_registry.TOP_IPS
    assert next(iter(result["by_source_ip"])) == HOST
    assert result["by_source_ip_omitted"] == 13 - tool_registry.TOP_IPS
    assert "by_destination_ip_omitted" not in result


@pytest.mark.asyncio
async def test_stats_count_in_sql_with_no_filters():
    _seed()

    stats, _ = await execute_backend_tool("get_findings_stats", {})

    assert stats["total_findings"] >= 35
    assert stats["by_severity"]["low"] >= 12
    assert "by_source_ip" not in stats
