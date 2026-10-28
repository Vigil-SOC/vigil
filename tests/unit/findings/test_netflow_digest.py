"""NetFlow digest at ingest and its case-wide sum (#3275)."""

from __future__ import annotations

import json

import pytest

from core.findings.netflow_digest import case_flow_summary, netflow_digest
from core.findings.source_evidence import (
    SOURCE_EVIDENCE_PREVIEW_LIMIT,
    loglm_row_evidence,
)
from core.ingestion.ingestion_service import IngestionService

pytestmark = pytest.mark.unit


def _flow(n, *, back_bytes=0, back_packets=0, src="10.0.0.1", dst="10.0.0.2"):
    return {
        "timestamp": 1_782_000_000 + n,
        "source_ip": src,
        "source_port": 40000 + n,
        "destination_ip": dst,
        "destination_port": 1 + n,
        "protocol": "TCP",
        "forward_packets": 1,
        "backward_packets": back_packets,
        "forward_bytes": 64,
        "backward_bytes": back_bytes,
    }


def test_ingest_digests_every_flow_not_the_preview():
    flows = [_flow(n) for n in range(170)]
    flows[150] = _flow(150, back_bytes=40, back_packets=1)
    flows[160] = _flow(160, back_packets=2)
    service = IngestionService.__new__(IngestionService)
    finding = service._parquet_row_to_finding(
        {
            "sequence_id": "seq-1",
            "source_evidence_kind": "netflow",
            "events_json": json.dumps(flows),
        }
    )

    context = finding["entity_context"]
    assert len(context["source_evidence"]["records"]) == SOURCE_EVIDENCE_PREVIEW_LIMIT
    digest = context["netflow_digest"]
    assert digest["flows"] == digest["total_flows"] == 170
    assert digest["complete"] is True
    assert digest["protocols"] == {"TCP": 170}
    assert digest["first"] < digest["last"]
    (pair,) = digest["pairs"]
    assert (pair["src"], pair["dst"]) == ("10.0.0.1", "10.0.0.2")
    assert pair["answered"] == 2  # both answers sit past the preview
    assert pair["bytes_back"] == sum(f["backward_bytes"] for f in flows) == 40
    assert pair["packets_back"] == 3
    assert pair["bytes_out"] == 64 * 170
    assert pair["port_count"] == 170
    assert pair["port_ranges"] == [[1, 170]]


def test_generic_evidence_gets_no_digest():
    _, digest = loglm_row_evidence({"events_json": [_flow(1)]})
    assert digest is None


def test_truncated_explicit_envelope_says_digest_is_partial():
    evidence, digest = loglm_row_evidence(
        {
            "source_evidence": {
                "version": 1,
                "telemetry_kind": "netflow",
                "schema_id": "netflow.v1",
                "status": "available",
                "provenance": "embedded",
                "total_records": 500,
                "truncated": True,
                "records": [_flow(n) for n in range(100)],
            }
        }
    )

    assert evidence["total_records"] == 500
    assert digest["flows"] == 100
    assert digest["total_flows"] == 500
    assert digest["complete"] is False


def test_missing_volume_fields_are_unknown_not_zero():
    flow = _flow(1)
    del flow["backward_bytes"], flow["backward_packets"]
    (pair,) = netflow_digest([flow, _flow(2)])["pairs"]

    assert pair["answered"] == 0
    assert pair["answer_unknown"] == 1
    assert pair["unknown"] == {"bytes_back": 1, "packets_back": 1}


def test_case_sums_digests_and_names_findings_without_one():
    first = netflow_digest([_flow(n) for n in range(150)])
    second = netflow_digest([_flow(n) for n in range(100, 300)])
    answered = netflow_digest([_flow(1, back_bytes=500, back_packets=3)])

    block = case_flow_summary(
        [("f-1", first), ("f-2", second), ("f-3", answered)],
        without_digest=["f-old"],
    )

    (pair,) = block["pairs"]
    assert pair["findings"] == 3
    assert pair["flows"] == 351
    assert pair["distinct_ports"] == 300  # union, not the sum of 351
    assert pair["answered"] == 1
    assert pair["bytes_back"] == 500
    assert pair["bytes_out"] == 64 * 351
    assert block["no_digest"]["finding_ids"] == ["f-old"]
    assert block["summary"].startswith("1 pair, 3 findings, 351 flows, 300 ports")


def test_case_block_stays_small_for_a_large_case():
    # 64 findings x 500 flows over many pairs and scattered ports.
    findings = []
    for n in range(64):
        flows = [
            _flow(i * 7 % 65000, src=f"10.0.{n % 30}.1", dst=f"10.1.{i % 40}.2")
            for i in range(500)
        ]
        findings.append((f"f-{n}", netflow_digest(flows)))
    missing = [f"old-{n}" for n in range(200)]

    block = case_flow_summary(findings, without_digest=missing)

    assert len(json.dumps(block, indent=2)) < 10_000
    assert block["pairs_omitted"] > 0
    assert block["no_digest"]["count"] == 200
    assert len(block["no_digest"]["finding_ids"]) == 20


# A reply on a host pair cut from the digest's pair list still counts.
def test_answer_on_a_capped_pair_is_not_lost():
    flows = [_flow(1, dst=f"10.9.0.{n}") for n in range(1, 255)]
    flows[200] = _flow(1, dst="10.9.0.201", back_bytes=500, back_packets=3)

    block = case_flow_summary([("f-1", netflow_digest(flows))])

    assert block["answered"] == 1
    assert block["bytes_back"] == 500
    assert block["distinct_ports"] == 1
    assert block["flows_outside_pairs"] == 234


def test_malformed_stored_digest_does_not_break_the_case():
    bad = {
        "flows": "many",
        "first": 5,
        "pairs": [
            {
                "src": "a",
                "dst": "b",
                "port_ranges": [["x", 2]],
                "top_ports": [[1, 2, 3]],
            },
            "junk",
        ],
    }
    good = netflow_digest([_flow(1)])

    block = case_flow_summary([("f-bad", bad), ("f-good", good), ("f-none", "x")])

    assert block["flows"] == 1
    assert block["distinct_ports_at_least"] is True
    assert block["no_digest"]["finding_ids"] == ["f-none"]


def test_huge_integers_read_as_unknown():
    flow = _flow(1)
    flow["backward_bytes"] = 10**400
    (pair,) = netflow_digest([flow])["pairs"]
    assert pair["unknown"] == {"bytes_back": 1}
