# A page of findings carries the hosts, users and indicators each one names (#2277), so a
# hunt over findings that name a host can rule on that host.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agents import tool_registry
from core.agents.tool_registry import execute_backend_tool
from core.cases.linked_entities import finding_entity_summary
from core.ingestion.ingestion_service import to_internal_finding

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
# The six-alert DUSKY LANTERN chain on FIN-WS-114, and the page search_findings returns
# for it. The agent layer's hunt test reads the same page, so this holds the two together.
CHAIN = ROOT / "services/agent/tests/fixtures/findings/dusky-lantern-chain.json"


def _chain() -> dict:
    return json.loads(CHAIN.read_text())


class _Findings:
    def __init__(self, findings):
        self.findings = findings

    def count_findings(self, **_kwargs):
        return len(self.findings)

    def get_findings(self, **_kwargs):
        return self.findings


@pytest.mark.asyncio
async def test_a_search_names_the_host_and_user_of_every_alert_in_the_chain(
    monkeypatch,
):
    chain = _chain()
    stored = [to_internal_finding(alert) for alert in chain["alerts"]]
    monkeypatch.setattr(tool_registry, "_data", lambda: _Findings(stored))

    page, handled = await execute_backend_tool("search_findings", {"query": ""})

    assert handled is True
    assert page == chain["page"]
    named = {row["finding_id"]: row["entities"] for row in page["findings"]}
    for finding_id in ("dl-002", "dl-003", "dl-004", "dl-005", "dl-006"):
        assert "FIN-WS-114" in named[finding_id]["hosts"]
    # The ISO email names its recipient, not a host.
    assert named["dl-001"]["users"] == ["priya.nair"]
    assert "hosts" not in named["dl-001"]


@pytest.mark.asyncio
async def test_the_raw_record_stays_behind_get_finding(monkeypatch):
    stored = [to_internal_finding(alert) for alert in _chain()["alerts"]]
    monkeypatch.setattr(tool_registry, "_data", lambda: _Findings(stored))

    page, _ = await execute_backend_tool("search_findings", {"query": ""})

    assert "source_evidence" not in json.dumps(page)
    assert "command_line" not in json.dumps(page)


def test_each_kind_is_bounded_so_a_page_of_twenty_stays_small():
    context = {
        "ips": [f"10.0.0.{n}" for n in range(40)],
        "hosts": ["h" * 1000],
        "users": ["a", "a", "b"],
        "source_evidence": {"records": [{"blob": "x" * 50_000}]},
    }

    summary = finding_entity_summary(context)

    assert summary["ips"] == [f"10.0.0.{n}" for n in range(5)]
    assert summary["truncated"] == ["ips"]
    assert summary["hosts"] == ["h" * 128]
    assert summary["users"] == ["a", "b"]
    assert len(json.dumps(summary)) < 1_000


def test_the_keys_sources_write_are_read_under_one_kind_each():
    context = {
        "hostnames": ["FIN-WS-114"],
        "usernames": ["priya.nair"],
        "src_ips": ["10.20.14.114"],
        "dest_ips": ["203.0.113.47"],
        "file_hashes": ["ab" * 32],
        "processes": ["rundll32.exe"],
    }

    summary = finding_entity_summary(context)

    assert summary == {
        "hosts": ["FIN-WS-114"],
        "users": ["priya.nair"],
        "ips": ["10.20.14.114", "203.0.113.47"],
        "hashes": ["ab" * 32],
        "processes": ["rundll32.exe"],
    }


def test_a_finding_with_no_context_names_nothing():
    assert finding_entity_summary(None) == {}
    assert finding_entity_summary("not a mapping") == {}
