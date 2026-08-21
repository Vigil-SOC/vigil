from __future__ import annotations

import copy

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from core.findings.evidence_store import (
    SequenceEvidenceBundle,
    SequenceEvidenceError,
    SequenceEvidenceStore,
)


def flow_rows() -> list[dict]:
    base = {
        "evidence_schema_version": "sequence_evidence/v1",
        "association_basis": "producer_membership",
        "sequence_id": "seq-1",
        "dataset_id": "ws3",
        "run_id": "run-1",
        "tenant": "tac",
        "focal_ip": "10.0.0.1",
        "engaged_ip": "10.0.0.2",
        "row_count": 2,
        "chunk_start_ms": 1_700_000_000_000,
        "chunk_end_ms": 1_700_000_001_000,
        "event_start_time": 1_700_000_000_000,
        "event_end_time": 1_700_000_001_000,
        "src_ip": "10.0.0.1",
        "dest_ip": "10.0.0.2",
        "src_port": 41000,
        "dest_port": 502,
        "fwd_bytes": 100.0,
        "bwd_bytes": 80.0,
        "fwd_pkts": 2,
        "bwd_pkts": 1,
        "flow_dur": 0.25,
        "protocol": "6",
        "source_file": "capture.parquet",
        "capture_id": "capture",
        "capture_sha256": "a" * 64,
        "first_packet": 1,
        "last_packet": 4,
    }
    return [
        {**base, "flow_id": "flow-1", "sequence_row_ordinal": 0, "timestamp_ms": 1_700_000_000_000},
        {**base, "flow_id": "flow-2", "sequence_row_ordinal": 1, "timestamp_ms": 1_700_000_001_000},
    ]


def modbus_rows() -> list[dict]:
    return [{
        "evidence_schema_version": "sequence_protocol_evidence/v1",
        "association_basis": "capture_packet_to_flow",
        "sequence_id": "seq-1",
        "flow_id": "flow-1",
        "dataset_id": "ws3",
        "run_id": "run-1",
        "capture_id": "capture",
        "capture_sha256": "a" * 64,
        "observation_ordinal": 0,
        "timestamp": 1_700_000_000_000_000,
        "client_ip": "10.0.0.1",
        "client_port": 41000,
        "server_ip": "10.0.0.2",
        "server_port": 502,
        "transaction_id": 7,
        "unit_id": 1,
        "function_code": 3,
        "function_name": "Read Holding Registers",
        "operation": "read",
        "address": 100,
        "quantity": 2,
        "response_status": "ok",
        "latency_usec": 250,
        "request_packet": 2,
        "response_packet": 3,
    }]


def write(path, rows) -> None:
    pq.write_table(pa.Table.from_pylist(rows), path)


def test_store_builds_deterministic_envelope_and_pages_records(tmp_path):
    flow_path = tmp_path / "flows.parquet"
    modbus_path = tmp_path / "modbus.parquet"
    write(flow_path, flow_rows())
    write(modbus_path, modbus_rows())

    bundle = SequenceEvidenceBundle(flow_path, modbus_path, expected_run_id="run-1")
    store = SequenceEvidenceStore(root=tmp_path / "store")
    stored = store.persist(bundle)
    envelope = bundle.envelope("seq-1", stored)

    assert envelope["version"] == 2
    assert envelope["summary"]["flow_count"] == 2
    assert envelope["summary"]["modbus_transaction_count"] == 1
    assert "Observed 2 exact flow records" in envelope["summary"]["text"]
    page = store.query(
        artifact_id=stored.artifact_id,
        sequence_id="seq-1",
        kind="netflow",
        offset=1,
        limit=1,
    )
    assert page["total"] == 2
    assert [record["flow_id"] for record in page["records"]] == ["flow-2"]


def test_bundle_fails_closed_on_inexact_membership(tmp_path):
    rows = copy.deepcopy(flow_rows())
    rows[1]["sequence_row_ordinal"] = 3
    flow_path = tmp_path / "bad.parquet"
    write(flow_path, rows)
    with pytest.raises(SequenceEvidenceError, match="membership mismatch"):
        SequenceEvidenceBundle(flow_path, None)


def test_bundle_rejects_mixed_exact_association_methods(tmp_path):
    rows = copy.deepcopy(flow_rows())
    rows[1]["association_basis"] = "sequence_builder_replay"
    flow_path = tmp_path / "mixed-association.parquet"
    write(flow_path, rows)
    with pytest.raises(SequenceEvidenceError, match="cannot mix"):
        SequenceEvidenceBundle(flow_path, None)


def test_bundle_rejects_fractional_integer_fields(tmp_path):
    rows = copy.deepcopy(flow_rows())
    rows[0]["sequence_row_ordinal"] = 0.5
    flow_path = tmp_path / "fractional-ordinal.parquet"
    write(flow_path, rows)
    with pytest.raises(SequenceEvidenceError, match="must be an integer"):
        SequenceEvidenceBundle(flow_path, None)


def test_bundle_rejects_protocol_flow_from_another_sequence(tmp_path):
    rows = copy.deepcopy(flow_rows())
    rows[0]["row_count"] = 1
    rows[1].update(sequence_id="seq-2", sequence_row_ordinal=0, row_count=1)
    for row in rows:
        row["chunk_start_ms"] = row["timestamp_ms"]
        row["chunk_end_ms"] = row["timestamp_ms"]
        row["event_start_time"] = row["timestamp_ms"]
        row["event_end_time"] = row["timestamp_ms"]
    protocol = copy.deepcopy(modbus_rows())
    protocol[0]["flow_id"] = "flow-2"
    flow_path = tmp_path / "flows.parquet"
    modbus_path = tmp_path / "modbus.parquet"
    write(flow_path, rows)
    write(modbus_path, protocol)
    with pytest.raises(SequenceEvidenceError, match="different sequence"):
        SequenceEvidenceBundle(flow_path, modbus_path)


def test_store_fails_closed_at_capacity(tmp_path):
    flow_path = tmp_path / "flows.parquet"
    write(flow_path, flow_rows())
    bundle = SequenceEvidenceBundle(flow_path, None)
    with pytest.raises(SequenceEvidenceError, match="capacity exceeded"):
        SequenceEvidenceStore(root=tmp_path / "store", max_bytes=1).persist(bundle)


def test_query_rechecks_finding_provenance_against_store_manifest(tmp_path):
    flow_path = tmp_path / "flows.parquet"
    write(flow_path, flow_rows())
    store = SequenceEvidenceStore(root=tmp_path / "store")
    stored = store.persist(SequenceEvidenceBundle(flow_path, None))
    with pytest.raises(SequenceEvidenceError, match="run provenance mismatch"):
        store.query(
            artifact_id=stored.artifact_id,
            sequence_id="seq-1",
            kind="netflow",
            offset=0,
            limit=10,
            expected_run_id="another-run",
        )
