from __future__ import annotations

from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq

from core.config import get_settings
from core.ingestion.ingestion_service import IngestionService


def test_companion_evidence_is_preflighted_and_merged_into_duplicate(tmp_path, monkeypatch):
    finding_path = tmp_path / "findings.parquet"
    flow_path = tmp_path / "sequence-evidence.parquet"
    protocol_path = tmp_path / "sequence-modbus-evidence.parquet"
    pq.write_table(pa.Table.from_pylist([{
        "sequence_id": "sequence-001",
        "row_count": 1,
        "event_start_time": 1_784_653_712_000,
        "event_end_time": 1_784_653_712_000,
        "embedding": [0.01, 0.02],
        "incident_pred": 0,
        "confidence_score": 0.93,
        "dataset_id": "ws3",
        "run_id": "run-001",
    }]), finding_path)
    pq.write_table(pa.Table.from_pylist([{
        "evidence_schema_version": "sequence_evidence/v1",
        "association_basis": "producer_membership",
        "sequence_id": "sequence-001",
        "sequence_row_ordinal": 0,
        "flow_id": "flow-001",
        "dataset_id": "ws3",
        "run_id": "run-001",
        "tenant": "tac",
        "focal_ip": "10.0.0.1",
        "engaged_ip": "10.0.0.2",
        "row_count": 1,
        "chunk_start_ms": 1_784_653_712_000,
        "chunk_end_ms": 1_784_653_712_000,
        "event_start_time": 1_784_653_712_000,
        "event_end_time": 1_784_653_712_000,
        "timestamp_ms": 1_784_653_712_000,
        "src_ip": "10.0.0.1",
        "dest_ip": "10.0.0.2",
        "src_port": 40000,
        "dest_port": 502,
        "fwd_bytes": 10.0,
        "bwd_bytes": 20.0,
        "fwd_pkts": 1,
        "bwd_pkts": 1,
        "flow_dur": 0.1,
        "protocol": "6",
        "source_file": "capture.parquet",
        "capture_id": "capture",
        "capture_sha256": "66" * 32,
        "first_packet": 1,
        "last_packet": 2,
    }]), flow_path)
    pq.write_table(pa.table({
        "evidence_schema_version": pa.array([], type=pa.string()),
        "association_basis": pa.array([], type=pa.string()),
        "sequence_id": pa.array([], type=pa.string()),
        "flow_id": pa.array([], type=pa.string()),
        "dataset_id": pa.array([], type=pa.string()),
        "run_id": pa.array([], type=pa.string()),
    }), protocol_path)

    existing = SimpleNamespace(entity_context={"sequence_id": "sequence-001"})

    class ExistingDatabase:
        def bulk_create_findings(self, findings):
            return {"imported": 0, "skipped": len(findings)}

        def get_finding(self, finding_id):
            return existing

        def update_finding(self, finding_id, **updates):
            existing.entity_context = updates["entity_context"]
            return True

    monkeypatch.setenv("EVIDENCE_STORE_PATH", str(tmp_path / "store"))
    get_settings.cache_clear()
    service = IngestionService.__new__(IngestionService)
    service._identity_warned = set()
    service.use_database = True
    service.db_service = ExistingDatabase()
    service.stats = {
        "findings_total": 0,
        "findings_imported": 0,
        "findings_skipped": 0,
        "findings_errors": 0,
        "cases_total": 0,
        "cases_imported": 0,
        "cases_skipped": 0,
        "cases_errors": 0,
        "evidence_attached": 0,
        "evidence_unchanged": 0,
    }
    stats = service.ingest_parquet_file(
        finding_path,
        evidence_file_path=flow_path,
        protocol_evidence_file_path=protocol_path,
        merge_source_evidence=True,
    )
    get_settings.cache_clear()

    assert stats["findings_skipped"] == 1
    assert stats["evidence_attached"] == 1
    assert existing.entity_context["source_evidence"]["version"] == 2
    assert existing.entity_context["source_evidence"]["summary"]["flow_count"] == 1


def test_new_finding_counts_atomically_inserted_evidence_as_attached():
    evidence = {
        "version": 2,
        "status": "available",
        "provenance": "joined",
        "association_basis": "producer_membership",
        "sequence_id": "sequence-001",
        "dataset_id": "ws3",
        "run_id": "run-001",
        "artifact": {
            "artifact_id": "aa" * 32,
            "flow_sha256": "bb" * 32,
            "modbus_sha256": None,
        },
        "summary": {"text": "Observed one exact flow."},
        "coverage": {},
        "streams": {
            "netflow": {
                "schema_id": "sequence_evidence/v1",
                "total_records": 1,
                "truncated": True,
                "records": [],
            },
            "modbus": {
                "schema_id": "sequence_protocol_evidence/v1",
                "total_records": 0,
                "truncated": False,
                "records": [],
            },
        },
    }

    class InsertDatabase:
        def bulk_create_findings(self, findings):
            return {
                "imported": 1,
                "skipped": 0,
                "imported_ids": [findings[0]["finding_id"]],
            }

        def get_finding(self, finding_id):  # pragma: no cover - must not be called
            raise AssertionError(f"unexpected duplicate lookup for {finding_id}")

    service = IngestionService.__new__(IngestionService)
    service.use_database = True
    service.db_service = InsertDatabase()
    service.stats = {
        "findings_total": 0,
        "findings_imported": 0,
        "findings_skipped": 0,
        "findings_errors": 0,
        "cases_total": 0,
        "cases_imported": 0,
        "cases_skipped": 0,
        "cases_errors": 0,
        "evidence_attached": 0,
        "evidence_unchanged": 0,
    }
    service._ingest_finding_batch(
        [{
            "finding_id": "finding-001",
            "timestamp": "2026-08-13T12:00:00Z",
            "anomaly_score": 0.1,
            "entity_context": {"source_evidence": evidence},
        }],
        merge_source_evidence=True,
    )

    assert service.stats["findings_imported"] == 1
    assert service.stats["evidence_attached"] == 1
    assert service.stats["evidence_unchanged"] == 0
