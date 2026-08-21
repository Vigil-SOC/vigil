"""Immutable, bounded storage and deterministic presentation for sequence evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from core.config import get_settings, vigil_path


FLOW_SCHEMA = "sequence_evidence/v1"
MODBUS_SCHEMA = "sequence_protocol_evidence/v1"
EVIDENCE_ENVELOPE_VERSION = 2
PREVIEW_LIMIT = 100
ALLOWED_FLOW_ASSOCIATIONS = {"producer_membership", "sequence_builder_replay"}
ARTIFACT_ID = re.compile(r"^[a-f0-9]{64}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


class SequenceEvidenceError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_ms(value: object) -> str:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _finite_number(value: object, default: float = 0.0) -> float:
    try:
        rendered = float(value)
    except (TypeError, ValueError):
        return default
    return rendered if math.isfinite(rendered) else default


def _required_int(
    value: object,
    label: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or (
        isinstance(value, float) and (not math.isfinite(value) or not value.is_integer())
    ):
        raise SequenceEvidenceError(f"{label} must be an integer")
    try:
        rendered = int(value)
    except (TypeError, ValueError) as exc:
        raise SequenceEvidenceError(f"{label} must be an integer") from exc
    if minimum is not None and rendered < minimum:
        raise SequenceEvidenceError(f"{label} must be at least {minimum}")
    if maximum is not None and rendered > maximum:
        raise SequenceEvidenceError(f"{label} must be at most {maximum}")
    return rendered


def _required_finite(value: object, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool):
        raise SequenceEvidenceError(f"{label} must be numeric")
    try:
        rendered = float(value)
    except (TypeError, ValueError) as exc:
        raise SequenceEvidenceError(f"{label} must be numeric") from exc
    if not math.isfinite(rendered) or rendered < minimum:
        raise SequenceEvidenceError(f"{label} must be finite and at least {minimum}")
    return rendered


def _endpoint_key(a_ip: object, a_port: object, b_ip: object, b_port: object) -> tuple:
    return tuple(
        sorted(
            (
                (
                    str(a_ip or ""),
                    _required_int(a_port, "endpoint port", minimum=0, maximum=65535),
                ),
                (
                    str(b_ip or ""),
                    _required_int(b_port, "endpoint port", minimum=0, maximum=65535),
                ),
            )
        )
    )


def _flow_record(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "flow_id": row.get("flow_id"),
        "sequence_row_ordinal": int(row.get("sequence_row_ordinal") or 0),
        "timestamp": _iso_ms(row["timestamp_ms"]),
        "source_ip": row.get("src_ip"),
        "source_port": row.get("src_port"),
        "destination_ip": row.get("dest_ip"),
        "destination_port": row.get("dest_port"),
        "protocol": row.get("protocol"),
        "forward_packets": int(row.get("fwd_pkts") or 0),
        "backward_packets": int(row.get("bwd_pkts") or 0),
        "forward_bytes": _finite_number(row.get("fwd_bytes")),
        "backward_bytes": _finite_number(row.get("bwd_bytes")),
        "duration_ms": round(_finite_number(row.get("flow_dur")) * 1000, 3),
        "capture_id": row.get("capture_id"),
        "capture_sha256": row.get("capture_sha256"),
        "first_packet": row.get("first_packet"),
        "last_packet": row.get("last_packet"),
    }


def _modbus_record(row: Mapping[str, Any]) -> Dict[str, Any]:
    timestamp = row.get("timestamp")
    timestamp_rendered = (
        _iso_ms(int(timestamp) // 1000)
        if timestamp is not None and abs(int(timestamp)) >= 100_000_000_000_000
        else _iso_ms(timestamp or 0)
    )
    keys = (
        "flow_id",
        "capture_id",
        "capture_sha256",
        "client_ip",
        "client_port",
        "server_ip",
        "server_port",
        "transaction_id",
        "unit_id",
        "function_code",
        "function_name",
        "operation",
        "address",
        "quantity",
        "write_address",
        "write_quantity",
        "register_values",
        "coil_values",
        "request_seen",
        "response_seen",
        "response_status",
        "exception_code",
        "exception_name",
        "latency_usec",
        "request_packet",
        "response_packet",
        "parser_warning",
    )
    return {"timestamp": timestamp_rendered, **{key: row.get(key) for key in keys}}


def _table_rows(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - deployment image includes pyarrow
        raise SequenceEvidenceError("pyarrow is required for sequence evidence") from exc
    return pq.ParquetFile(path).read().to_pylist()


@dataclass(frozen=True)
class StoredEvidence:
    artifact_id: str
    flow_sha256: str
    modbus_sha256: Optional[str]
    root: Path

    def public_ref(self) -> Dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "flow_sha256": self.flow_sha256,
            "modbus_sha256": self.modbus_sha256,
        }


class SequenceEvidenceBundle:
    """Validated run evidence, grouped by exact producer sequence identity."""

    def __init__(
        self,
        flow_path: Path,
        modbus_path: Optional[Path],
        *,
        expected_run_id: Optional[str] = None,
    ) -> None:
        self.flow_path = Path(flow_path)
        self.modbus_path = Path(modbus_path) if modbus_path else None
        self.flow_rows = _table_rows(self.flow_path)
        self.modbus_rows = _table_rows(self.modbus_path) if self.modbus_path else []
        self.flows_by_sequence: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.modbus_by_sequence: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._validate(expected_run_id)

    def _validate(self, expected_run_id: Optional[str]) -> None:
        if not self.flow_rows:
            raise SequenceEvidenceError("sequence evidence contains no flow rows")
        seen_flow_ids: set[str] = set()
        flow_sequence: dict[str, str] = {}
        flow_by_id: dict[str, dict[str, Any]] = {}
        run_ids: set[str] = set()
        dataset_ids: set[str] = set()
        tenant_ids: set[str] = set()
        association_bases: set[str] = set()
        for row in self.flow_rows:
            if row.get("evidence_schema_version") != FLOW_SCHEMA:
                raise SequenceEvidenceError("unsupported flow evidence schema")
            association_basis = str(row.get("association_basis") or "")
            if association_basis not in ALLOWED_FLOW_ASSOCIATIONS:
                raise SequenceEvidenceError("flow evidence is not exact sequence membership")
            association_bases.add(association_basis)
            sequence_id = str(row.get("sequence_id") or "")
            flow_id = str(row.get("flow_id") or "")
            if not sequence_id or not flow_id or flow_id in seen_flow_ids:
                raise SequenceEvidenceError("flow evidence has missing/duplicate identities")
            _required_int(row.get("sequence_row_ordinal"), "sequence_row_ordinal", minimum=0)
            _required_int(row.get("row_count"), "row_count", minimum=1)
            chunk_start = _required_int(row.get("chunk_start_ms"), "chunk_start_ms")
            chunk_end = _required_int(
                row.get("chunk_end_ms"), "chunk_end_ms", minimum=chunk_start
            )
            event_start = _required_int(row.get("event_start_time"), "event_start_time")
            event_end = _required_int(
                row.get("event_end_time"), "event_end_time", minimum=event_start
            )
            timestamp_ms = _required_int(row.get("timestamp_ms"), "timestamp_ms")
            _iso_ms(timestamp_ms)
            if not event_start <= timestamp_ms <= event_end:
                raise SequenceEvidenceError("flow timestamp is outside event coverage")
            if not str(row.get("src_ip") or "") or not str(row.get("dest_ip") or ""):
                raise SequenceEvidenceError("flow evidence has a missing endpoint")
            _required_int(row.get("src_port"), "src_port", minimum=0, maximum=65535)
            _required_int(row.get("dest_port"), "dest_port", minimum=0, maximum=65535)
            _required_int(row.get("fwd_pkts"), "fwd_pkts", minimum=0)
            _required_int(row.get("bwd_pkts"), "bwd_pkts", minimum=0)
            _required_finite(row.get("fwd_bytes"), "fwd_bytes")
            _required_finite(row.get("bwd_bytes"), "bwd_bytes")
            _required_finite(row.get("flow_dur"), "flow_dur")
            capture_sha = row.get("capture_sha256")
            if capture_sha is not None and not SHA256.fullmatch(str(capture_sha)):
                raise SequenceEvidenceError("flow evidence has an invalid capture SHA-256")
            first_packet = row.get("first_packet")
            last_packet = row.get("last_packet")
            if (first_packet is None) != (last_packet is None):
                raise SequenceEvidenceError("flow packet provenance is incomplete")
            if first_packet is not None:
                first = _required_int(first_packet, "first_packet", minimum=1)
                last = _required_int(last_packet, "last_packet", minimum=first)
                if not str(row.get("capture_id") or "") or capture_sha is None:
                    raise SequenceEvidenceError(
                        "packet-addressable flow evidence lacks capture provenance"
                    )
                if last < first:  # pragma: no cover - guarded by minimum
                    raise SequenceEvidenceError("flow packet provenance is reversed")
            seen_flow_ids.add(flow_id)
            flow_sequence[flow_id] = sequence_id
            flow_by_id[flow_id] = row
            run_ids.add(str(row.get("run_id") or ""))
            dataset_ids.add(str(row.get("dataset_id") or ""))
            tenant = str(row.get("tenant") or "")
            focal_ip = str(row.get("focal_ip") or "")
            engaged_ip = str(row.get("engaged_ip") or "")
            if not tenant or not focal_ip or not engaged_ip:
                raise SequenceEvidenceError(
                    "flow evidence lacks tenant or sequence endpoint identity"
                )
            if {focal_ip, engaged_ip} != {
                str(row.get("src_ip")),
                str(row.get("dest_ip")),
            }:
                raise SequenceEvidenceError(
                    "flow endpoints do not match the sequence endpoint pair"
                )
            tenant_ids.add(tenant)
            self.flows_by_sequence[sequence_id].append(row)
        if len(run_ids) != 1 or "" in run_ids:
            raise SequenceEvidenceError(f"flow evidence must contain one run_id: {run_ids}")
        if len(dataset_ids) != 1 or "" in dataset_ids:
            raise SequenceEvidenceError(
                f"flow evidence must contain one dataset_id: {dataset_ids}"
            )
        if len(tenant_ids) != 1:
            raise SequenceEvidenceError(
                f"flow evidence must contain one tenant: {tenant_ids}"
            )
        if len(association_bases) != 1:
            raise SequenceEvidenceError(
                "flow evidence cannot mix exact association methods"
            )
        self.run_id = next(iter(run_ids))
        self.dataset_id = next(iter(dataset_ids))
        self.tenant = next(iter(tenant_ids))
        self.association_basis = next(iter(association_bases))
        if expected_run_id and self.run_id != expected_run_id:
            raise SequenceEvidenceError(
                f"evidence run_id mismatch: {self.run_id} != {expected_run_id}"
            )
        for sequence_id, rows in self.flows_by_sequence.items():
            rows.sort(key=lambda row: int(row.get("sequence_row_ordinal") or 0))
            ordinals = [int(row.get("sequence_row_ordinal") or 0) for row in rows]
            declared = {int(row.get("row_count") or 0) for row in rows}
            if ordinals != list(range(len(rows))) or declared != {len(rows)}:
                raise SequenceEvidenceError(
                    f"flow evidence membership mismatch for {sequence_id}"
                )
            metadata = {
                (
                    str(row.get("focal_ip") or ""),
                    str(row.get("engaged_ip") or ""),
                    _required_int(row.get("chunk_start_ms"), "chunk_start_ms"),
                    _required_int(row.get("chunk_end_ms"), "chunk_end_ms"),
                    _required_int(row.get("event_start_time"), "event_start_time"),
                    _required_int(row.get("event_end_time"), "event_end_time"),
                )
                for row in rows
            }
            if len(metadata) != 1:
                raise SequenceEvidenceError(
                    f"flow evidence metadata mismatch for {sequence_id}"
                )
            _, _, chunk_start, chunk_end, event_start, event_end = next(iter(metadata))
            timestamps = [int(row["timestamp_ms"]) for row in rows]
            if (
                timestamps[0] != chunk_start
                or timestamps[-1] != chunk_end
                or min(timestamps) != event_start
                or max(timestamps) != event_end
            ):
                raise SequenceEvidenceError(
                    f"flow evidence coverage mismatch for {sequence_id}"
                )

        known_flow_ids = seen_flow_ids
        seen_observations: set[tuple[str, int]] = set()
        for row in self.modbus_rows:
            if row.get("evidence_schema_version") != MODBUS_SCHEMA:
                raise SequenceEvidenceError("unsupported Modbus evidence schema")
            if row.get("association_basis") != "capture_packet_to_flow":
                raise SequenceEvidenceError("Modbus evidence is not packet-associated")
            sequence_id = str(row.get("sequence_id") or "")
            if sequence_id not in self.flows_by_sequence:
                raise SequenceEvidenceError(
                    f"Modbus evidence references unknown sequence: {sequence_id}"
                )
            flow_id = str(row.get("flow_id") or "")
            if flow_id not in known_flow_ids:
                raise SequenceEvidenceError("Modbus evidence references unknown flow_id")
            if flow_sequence[flow_id] != sequence_id:
                raise SequenceEvidenceError(
                    "Modbus evidence flow_id belongs to a different sequence"
                )
            if str(row.get("run_id") or "") != self.run_id:
                raise SequenceEvidenceError("Modbus evidence run_id mismatch")
            if str(row.get("dataset_id") or "") != self.dataset_id:
                raise SequenceEvidenceError("Modbus evidence dataset_id mismatch")
            flow = flow_by_id[flow_id]
            if str(row.get("capture_id") or "") != str(flow.get("capture_id") or ""):
                raise SequenceEvidenceError("Modbus evidence capture_id mismatch")
            if str(row.get("capture_sha256") or "") != str(
                flow.get("capture_sha256") or ""
            ):
                raise SequenceEvidenceError("Modbus evidence capture SHA-256 mismatch")
            if _endpoint_key(
                row.get("client_ip"),
                row.get("client_port"),
                row.get("server_ip"),
                row.get("server_port"),
            ) != _endpoint_key(
                flow.get("src_ip"),
                flow.get("src_port"),
                flow.get("dest_ip"),
                flow.get("dest_port"),
            ):
                raise SequenceEvidenceError("Modbus evidence endpoint mismatch")
            packet_refs = [
                _required_int(value, label, minimum=1)
                for label, value in (
                    ("request_packet", row.get("request_packet")),
                    ("response_packet", row.get("response_packet")),
                )
                if value is not None
            ]
            if not packet_refs or flow.get("first_packet") is None:
                raise SequenceEvidenceError("Modbus evidence lacks exact packet provenance")
            first = int(flow["first_packet"])
            last = int(flow["last_packet"])
            if any(packet < first or packet > last for packet in packet_refs):
                raise SequenceEvidenceError("Modbus packet reference is outside its flow")
            capture_id = str(row.get("capture_id") or "")
            ordinal = _required_int(
                row.get("observation_ordinal"), "observation_ordinal", minimum=0
            )
            observation_id = (capture_id, ordinal)
            if observation_id in seen_observations:
                raise SequenceEvidenceError("Modbus evidence has a duplicate observation")
            seen_observations.add(observation_id)
            _required_int(row.get("timestamp"), "Modbus timestamp")
            _modbus_record(row)
            self.modbus_by_sequence[sequence_id].append(row)

    def has_sequence(self, sequence_id: str, expected_rows: Optional[int] = None) -> bool:
        rows = self.flows_by_sequence.get(sequence_id, [])
        return bool(rows) and (expected_rows is None or len(rows) == expected_rows)

    def envelope(self, sequence_id: str, stored: StoredEvidence) -> Dict[str, Any]:
        flows = self.flows_by_sequence.get(sequence_id)
        if not flows:
            raise SequenceEvidenceError(f"no exact evidence for sequence {sequence_id}")
        modbus = self.modbus_by_sequence.get(sequence_id, [])
        flow_preview = [_flow_record(row) for row in flows[:PREVIEW_LIMIT]]
        modbus_preview = [_modbus_record(row) for row in modbus[:PREVIEW_LIMIT]]
        packet_count = sum(
            int(row.get("fwd_pkts") or 0) + int(row.get("bwd_pkts") or 0)
            for row in flows
        )
        byte_count = sum(
            _finite_number(row.get("fwd_bytes")) + _finite_number(row.get("bwd_bytes"))
            for row in flows
        )
        protocol_counts = Counter(str(row.get("protocol") or "unknown") for row in flows)
        operation_counts = Counter(str(row.get("operation") or "unknown") for row in modbus)
        response_counts = Counter(str(row.get("response_status") or "unknown") for row in modbus)
        timestamps = [int(row["timestamp_ms"]) for row in flows]
        capture_hashes = sorted(
            {str(row["capture_sha256"]) for row in flows if row.get("capture_sha256")}
        )
        summary_text = (
            f"Observed {len(flows)} exact flow records carrying {packet_count} packets "
            f"and {int(byte_count)} bytes."
        )
        if modbus:
            summary_text += (
                f" The same sequence contains {len(modbus)} packet-associated Modbus "
                f"transactions ({operation_counts.get('read', 0)} reads, "
                f"{operation_counts.get('write', 0)} writes)."
            )
        return {
            "version": EVIDENCE_ENVELOPE_VERSION,
            "status": "available",
            "provenance": "joined",
            "association_basis": str(flows[0]["association_basis"]),
            "sequence_id": sequence_id,
            "dataset_id": self.dataset_id,
            "run_id": self.run_id,
            "artifact": stored.public_ref(),
            "summary": {
                "text": summary_text,
                "flow_count": len(flows),
                "packet_count": packet_count,
                "byte_count": int(byte_count),
                "protocol_counts": dict(sorted(protocol_counts.items())),
                "modbus_transaction_count": len(modbus),
                "modbus_operation_counts": dict(sorted(operation_counts.items())),
                "modbus_response_counts": dict(sorted(response_counts.items())),
            },
            "coverage": {
                "event_start_time": min(timestamps),
                "event_end_time": max(timestamps),
                "capture_sha256s": capture_hashes,
                "flow_membership_complete": True,
                "modbus_packet_association": True,
            },
            "streams": {
                "netflow": {
                    "schema_id": FLOW_SCHEMA,
                    "total_records": len(flows),
                    "truncated": len(flows) > len(flow_preview),
                    "records": flow_preview,
                },
                "modbus": {
                    "schema_id": MODBUS_SCHEMA,
                    "total_records": len(modbus),
                    "truncated": len(modbus) > len(modbus_preview),
                    "records": modbus_preview,
                },
            },
        }


class SequenceEvidenceStore:
    def __init__(self, root: Optional[Path] = None, max_bytes: Optional[int] = None) -> None:
        settings = get_settings()
        configured = str(getattr(settings, "evidence_store_path", "") or "").strip()
        self.root = Path(root or configured or vigil_path("evidence", write=True))
        configured_max = int(getattr(settings, "evidence_store_max_mb", 10_240)) * 1024 * 1024
        self.max_bytes = configured_max if max_bytes is None else int(max_bytes)
        if self.max_bytes < 1:
            raise SequenceEvidenceError("evidence store size limit must be positive")

    def _stored_bytes(self) -> int:
        if not self.root.exists():
            return 0
        return sum(
            path.stat().st_size
            for path in self.root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def persist(self, bundle: SequenceEvidenceBundle) -> StoredEvidence:
        flow_sha = _sha256(bundle.flow_path)
        modbus_sha = _sha256(bundle.modbus_path) if bundle.modbus_path else None
        digest = hashlib.sha256(
            f"vigil-sequence-evidence/v1\0{bundle.run_id}\0{flow_sha}\0{modbus_sha or ''}".encode()
        ).hexdigest()
        destination = self.root / digest
        files = [(bundle.flow_path, destination / "sequence-evidence.parquet", flow_sha)]
        if bundle.modbus_path:
            files.append(
                (bundle.modbus_path, destination / "sequence-modbus-evidence.parquet", modbus_sha)
            )
        manifest = {
            "schema": "vigil_sequence_evidence_store/v1",
            "artifact_id": digest,
            "dataset_id": bundle.dataset_id,
            "run_id": bundle.run_id,
            "flow_sha256": flow_sha,
            "modbus_sha256": modbus_sha,
            "sequence_count": len(bundle.flows_by_sequence),
            "flow_count": len(bundle.flow_rows),
            "modbus_count": len(bundle.modbus_rows),
        }
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        manifest_path = destination / "manifest.json"
        additional_bytes = sum(
            source.stat().st_size for source, target, _ in files if not target.exists()
        )
        if not manifest_path.exists():
            additional_bytes += len(manifest_bytes)
        if self._stored_bytes() + additional_bytes > self.max_bytes:
            raise SequenceEvidenceError(
                "evidence store capacity exceeded; apply the governed evidence retention procedure"
            )
        destination.mkdir(parents=True, exist_ok=True)
        for source, target, expected_sha in files:
            assert expected_sha is not None
            if target.exists():
                if _sha256(target) != expected_sha:
                    raise SequenceEvidenceError(f"immutable evidence collision: {target}")
                continue
            temporary = target.with_suffix(target.suffix + f".{os.getpid()}.partial")
            shutil.copyfile(source, temporary)
            if _sha256(temporary) != expected_sha:
                temporary.unlink(missing_ok=True)
                raise SequenceEvidenceError("evidence copy hash mismatch")
            os.replace(temporary, target)
            os.chmod(target, 0o440)
        if manifest_path.exists():
            try:
                existing_manifest = json.loads(manifest_path.read_text())
            except (OSError, ValueError) as exc:
                raise SequenceEvidenceError(
                    f"invalid immutable evidence manifest: {manifest_path}"
                ) from exc
            if existing_manifest != manifest:
                raise SequenceEvidenceError(
                    f"immutable evidence manifest collision: {manifest_path}"
                )
        else:
            temporary = manifest_path.with_suffix(".json.partial")
            temporary.write_bytes(manifest_bytes)
            os.replace(temporary, manifest_path)
            os.chmod(manifest_path, 0o440)
        os.chmod(destination, 0o550)
        return StoredEvidence(digest, flow_sha, modbus_sha, destination)

    def query(
        self,
        *,
        artifact_id: str,
        sequence_id: str,
        kind: str,
        offset: int,
        limit: int,
        expected_run_id: Optional[str] = None,
        expected_dataset_id: Optional[str] = None,
        expected_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not ARTIFACT_ID.fullmatch(artifact_id):
            raise SequenceEvidenceError("invalid evidence artifact identity")
        if not sequence_id:
            raise SequenceEvidenceError("sequence_id is required")
        if kind not in {"netflow", "modbus"}:
            raise SequenceEvidenceError("kind must be netflow or modbus")
        if offset < 0 or limit < 1 or limit > 500:
            raise SequenceEvidenceError("invalid evidence page bounds")
        manifest_path = self.root / artifact_id / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, ValueError) as exc:
            raise SequenceEvidenceError("evidence store manifest is missing or invalid") from exc
        if manifest.get("artifact_id") != artifact_id:
            raise SequenceEvidenceError("evidence store artifact identity mismatch")
        if expected_run_id and manifest.get("run_id") != expected_run_id:
            raise SequenceEvidenceError("evidence store run provenance mismatch")
        if expected_dataset_id and manifest.get("dataset_id") != expected_dataset_id:
            raise SequenceEvidenceError("evidence store dataset provenance mismatch")
        filename = (
            "sequence-evidence.parquet"
            if kind == "netflow"
            else "sequence-modbus-evidence.parquet"
        )
        path = self.root / artifact_id / filename
        expected_sha = (
            manifest.get("flow_sha256")
            if kind == "netflow"
            else manifest.get("modbus_sha256")
        )
        if expected_sha256 and expected_sha != expected_sha256:
            raise SequenceEvidenceError("evidence store artifact hash mismatch")
        if expected_sha is None and kind == "modbus":
            return {"kind": kind, "total": 0, "offset": offset, "limit": limit, "records": []}
        if not path.is_file() or _sha256(path) != expected_sha:
            raise SequenceEvidenceError("evidence store file is missing or corrupt")
        try:
            import pyarrow.dataset as ds
        except ImportError as exc:  # pragma: no cover - deployment image includes pyarrow
            raise SequenceEvidenceError("pyarrow is required for sequence evidence") from exc
        rows = ds.dataset(path, format="parquet").to_table(
            filter=ds.field("sequence_id") == sequence_id
        ).to_pylist()
        if kind == "netflow":
            rows.sort(key=lambda row: int(row.get("sequence_row_ordinal") or 0))
            normalize = _flow_record
        else:
            rows.sort(
                key=lambda row: (
                    int(row.get("timestamp") or 0),
                    int(row.get("observation_ordinal") or 0),
                )
            )
            normalize = _modbus_record
        return {
            "kind": kind,
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "records": [normalize(row) for row in rows[offset : offset + limit]],
        }
