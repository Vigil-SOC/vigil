# Structured source evidence

Vigil can retain a bounded preview of the source records that produced a
finding. The preview lives at `finding.entity_context.source_evidence`, so it
does not require a database migration. Finding list responses retain only the
envelope metadata with `payload_included: false`;
`GET /api/findings/{finding_id}` returns the payload.

## Contract

```json
{
  "version": 1,
  "telemetry_kind": "netflow",
  "schema_id": "netflow.v1",
  "status": "available",
  "provenance": "embedded",
  "total_records": 150,
  "truncated": true,
  "records": [],
  "raw_text": "optional source-native text"
}
```

`telemetry_kind` is the renderer selector, not `finding.data_source`. Supported
values are `netflow`, `dns`, `http_session`, and `generic_log`. This lets the
capability apply to network flows, DNS, Layer 7 sessions, and other logs without
forcing unrelated schemas into a NetFlow table. `schema_id` remains owned by
the producer and should be versioned when its record fields change.

`status` is one of:

- `available`: at least one structured record or non-empty `raw_text` exists.
- `not_in_artifact`: the source artifact did not include raw evidence.
- `redacted`: evidence existed but was intentionally removed before ingestion.
- `invalid`: evidence was supplied but failed contract validation.

No envelope means the finding has no declared source-evidence capability, so
the finding dialog hides the section. The other non-available states render a
short, truthful message.

## LogLM Parquet inputs

The preferred Parquet column is `source_evidence`, containing the object above
or its JSON representation. Current artifacts with `events_json` and/or
`sequence` are adapted during ingestion:

- `events_json` becomes ordered `records`.
- `sequence` becomes `raw_text`.
- `source_evidence_kind` explicitly selects the telemetry renderer. If absent,
  these legacy columns use `generic_log` rather than guessing from
  `data_source="flow"`.
- Optional columns are `source_evidence_schema_id`, `source_evidence_status`,
  `source_evidence_provenance`, and `source_evidence_total_records`.

Ingestion retains at most 100 structured records and 64 KiB of raw text per
finding, records truncation metadata, converts non-finite numbers to `null`,
and never lets malformed evidence prevent the finding itself from ingesting.

## Canonical record fields

The built-in tables recognize these v1 field names:

- NetFlow: `timestamp`, `source_ip`, `source_port`, `destination_ip`,
  `destination_port`, `protocol`, `forward_packets`, `backward_packets`,
  `forward_bytes`, `backward_bytes`, `duration_ms`.
- DNS: `timestamp`, `client_ip`, `server_ip`, `query`, `query_type`, `answer`,
  `response_code`, `ttl`.

HTTP-session and generic-log records render as ordered, expandable key/value
records so source fields remain visible without inventing a universal schema.
