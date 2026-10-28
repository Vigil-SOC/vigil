# A native tool's result fits result_cap once rendered (#3276): the agent reads the
# whole answer, the stored triage included, rather than a prefix cut inside a value.

from __future__ import annotations

import json

import pytest

from core.agents import tool_registry
from core.agents.tool_registry import PAGE_LIMIT, execute_backend_tool
from core.ingestion.ingestion_service import IngestionService

pytestmark = pytest.mark.unit

RESULT_CAP = 20_000  # core/workflows/playbook_resolver.py DEFAULT_RUNTIME


# As security.ts renders it: compact JSON of the rows under a one-line header.
def _rendered(result) -> str:
    rows = json.dumps([result], ensure_ascii=False, separators=(",", ":"), default=str)
    return f"1 row(s) from vigil\n{rows}"


# A port scan from one host across 40 peers: 170 flows, a 100-record preview,
# ports scattered so every pair stores many port ranges.
def _netflow_finding() -> dict:
    flows = [
        {
            "timestamp": 1_782_000_000 + n,
            "source_ip": "10.20.30.40",
            "source_port": 40000 + n,
            "destination_ip": f"172.16.{n % 40}.{n % 7 + 1}",
            "destination_port": 1 + n * 3,
            "protocol": "TCP",
            "forward_packets": 2,
            "backward_packets": n % 3,
            "forward_bytes": 120,
            "backward_bytes": 60 * (n % 3),
            "duration_ms": 12 + n,
            "tcp_flags": "SYN",
            "sensor": "edge-fw-01.corp.example",
        }
        for n in range(170)
    ]
    service = IngestionService.__new__(IngestionService)
    finding = service._parquet_row_to_finding(
        {
            "sequence_id": "seq-scan-1",
            "source_evidence_kind": "netflow",
            "events_json": json.dumps(flows),
        }
    )
    finding.update(
        finding_id="f-netflow-1",
        title="LogLM anomaly: horizontal TCP scan from 10.20.30.40",
        severity="high",
        ai_enrichment={
            "ai_triage": {
                "timestamp": "2026-10-27T12:00:00+00:00",
                "result": {
                    "severity": "high",
                    "confidence": 0.82,
                    "category": "reconnaissance",
                    "recommended_action": "investigate",
                    "reasoning": "One host touched 40 peers on 170 ports in 3 minutes.",
                },
            },
            "triage_confidence": 0.82,
            "category": "reconnaissance",
            "recommended_action": "investigate",
            "triage_reasoning": "One host touched 40 peers on 170 ports in 3 minutes.",
        },
    )
    return finding


class _Data:
    def __init__(self, findings, total=None):
        self.findings = findings
        self.total = len(findings) if total is None else total

    def get_finding(self, finding_id):
        return next(f for f in self.findings if f["finding_id"] == finding_id)

    def get_findings_summary(self, **_kwargs):
        ips = {f"10.0.{n}.{n}": 50 for n in range(10)}
        return {
            "total": self.total,
            "by_severity": {"high": 60, "medium": 40},
            "by_data_source": {"netflow": 100},
            "by_state": {"open": 90, "dismissed": 10},
            "by_source_ip": ips,
            "by_destination_ip": ips,
            "by_source_ip_omitted": 30,
            "by_destination_ip_omitted": 30,
        }

    def get_findings(self, *, limit, offset, **_kwargs):
        return self.findings[offset : offset + limit]


@pytest.fixture(autouse=True)
def _no_states(monkeypatch):
    import core.findings.alert_outcomes as outcomes

    monkeypatch.setattr(outcomes, "stored_finding_states", lambda *_: {})


@pytest.mark.asyncio
async def test_get_finding_on_a_netflow_finding_fits_and_leads_with_its_triage(
    monkeypatch,
):
    finding = _netflow_finding()
    assert len(_rendered(finding)) > RESULT_CAP  # what the agent got before
    monkeypatch.setattr(tool_registry, "_data", lambda: _Data([finding]))

    shaped, handled = await execute_backend_tool(
        "get_finding", {"finding_id": "f-netflow-1"}
    )

    assert handled is True
    assert len(_rendered(shaped)) < RESULT_CAP
    keys = list(shaped)
    assert keys[:3] == ["finding_id", "title", "severity"]
    assert keys[-1] == "entity_context"
    assert shaped["triage"]["category"] == "reconnaissance"
    assert shaped["triage"]["reasoning"].startswith("One host touched 40 peers")
    assert shaped["triage"]["triaged_at"] == "2026-10-27T12:00:00+00:00"
    assert "ai_enrichment" not in shaped  # nothing left beside the triage
    context = shaped["entity_context"]
    evidence = context["source_evidence"]
    assert len(evidence["records"]) == tool_registry.SAMPLE_RECORDS
    assert evidence["total_records"] == 170
    assert evidence["records_shown"].startswith("5 of the 100 stored")
    assert context["netflow_digest"]["flows"] == 170
    # The stored finding is untouched: the REST endpoints still serve it whole.
    assert len(finding["entity_context"]["source_evidence"]["records"]) == 100


@pytest.mark.asyncio
async def test_search_findings_at_its_maximum_limit_fits(monkeypatch):
    findings = [
        {**_netflow_finding(), "finding_id": f"f-{n:03}", "description": "d" * 400}
        for n in range(100)
    ]
    monkeypatch.setattr(tool_registry, "_data", lambda: _Data(findings))

    page, _ = await execute_backend_tool(
        "search_findings", {"query": "10.20.30.40", "limit": 100}
    )

    assert len(_rendered(page)) < RESULT_CAP
    assert page["limit"] == PAGE_LIMIT
    assert page["returned"] == len(page["findings"]) == PAGE_LIMIT
    assert page["has_more"] is True
    assert page["next_offset"] == PAGE_LIMIT


# Summaries naming as many entities as they can stop the page short of its limit,
# and the paging says where the next one starts.
@pytest.mark.asyncio
async def test_a_page_too_big_for_the_cap_stops_short_and_says_where_to_resume(
    monkeypatch,
):
    names = {
        key: [f"{key}-{n}-" + "z" * 120 for n in range(5)]
        for key in ("hostnames", "usernames", "src_ips", "domains", "urls")
    }
    findings = [
        {"finding_id": f"f-{n:03}", "title": "t", "entity_context": names}
        for n in range(30)
    ]
    monkeypatch.setattr(tool_registry, "_data", lambda: _Data(findings))

    page, _ = await execute_backend_tool("search_findings", {"query": "", "limit": 25})

    assert len(_rendered(page)) < RESULT_CAP
    assert 0 < page["returned"] < 25
    assert page["has_more"] is True
    assert page["next_offset"] == page["returned"]

    rest, _ = await execute_backend_tool(
        "search_findings", {"query": "", "limit": 25, "offset": 28}
    )
    assert rest["returned"] == 2
    assert rest["has_more"] is False
    assert "next_offset" not in rest
