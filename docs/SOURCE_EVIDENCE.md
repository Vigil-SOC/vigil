# Structured source evidence

Vigil supports two source-evidence contracts inside
`finding.entity_context.source_evidence`. Finding-list responses retain only
the envelope; finding detail and the evidence paging endpoint expose records.

## Embedded evidence (version 1)

Version 1 accepts a bounded producer-owned preview for NetFlow, DNS, HTTP
sessions, or generic logs. It records a versioned schema ID, availability
state, provenance, total record count, truncation, and up to 100 records. It is
appropriate when the finding artifact itself carries the source preview.

## Exact sequence evidence (version 2)

For LogLM findings, ingestion may receive two immutable companion Parquet
artifacts:

- `sequence_evidence/v1`: normalized flow rows with exact `sequence_id`,
  ordered membership, flow identity, run/dataset identity, endpoints, counters,
  capture hash, and packet bounds.
- `sequence_protocol_evidence/v1`: Modbus observations joined to one of those
  flows by capture, endpoints, and packet references.

Vigil validates all identities and joins before mutating findings. It then
stores the companions under a content-addressed artifact ID and attaches a
version-2 envelope to each finding. Duplicate finding imports may update that
evidence reference but do not rewrite the finding verdict or identity.

`GET /api/findings/{finding_id}/source-evidence` accepts `kind=netflow|modbus`,
`offset`, and `limit` (maximum 500). The UI renders a deterministic behavioral
summary plus paginated flow and Modbus tables. Any LLM-generated prose remains
separate from these source-backed facts.

The store defaults to 10,240 MiB and is configured by
`EVIDENCE_STORE_PATH`/`EVIDENCE_STORE_MAX_MB`. It fails closed when capacity is
exhausted; it does not evict artifacts still referenced by findings. Companion
files contain normalized metadata and provenance, never raw PCAP or payload.

## Truth boundaries

- A model finding is not proof of attack.
- Observed endpoint traffic is not an inferred network topology.
- Missing evidence remains unavailable; Vigil does not reconstruct records
  from token bins or ask an LLM to invent them.
- Capture hashes and packet references prove artifact linkage only when the
  upstream custody and manifest gates also pass.
