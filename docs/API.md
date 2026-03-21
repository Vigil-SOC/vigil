# API Reference

## Data Model

### Finding

Primary security observation with embeddings and MITRE predictions.

```json
{
  "finding_id": "f-2024-01-15-001",
  "embedding": [0.123, -0.456, 0.789, 0.012],
  "mitre_predictions": {"T1071.001": 0.85, "T1048.003": 0.72},
  "anomaly_score": 0.92,
  "entity_context": {
    "src_ip": "10.0.1.15",
    "dst_ip": "203.0.113.50",
    "hostname": "workstation-042",
    "user": "jsmith"
  },
  "timestamp": "2024-01-15T14:32:18Z",
  "data_source": "flow",
  "severity": "high",
  "status": "new"
}
```

> **Note:** `embedding` is a numeric array (typically 1536 floats for OpenAI embeddings or 1024 for Claude); only the first few values are shown above.

### Case

Investigation grouping related findings.

```json
{
  "case_id": "case-2024-01-15-001",
  "title": "Potential C2 Beaconing",
  "finding_ids": ["f-2024-01-15-001", "f-2024-01-15-002"],
  "status": "investigating",
  "priority": "high",
  "assignee": "analyst@example.com",
  "notes": [],
  "timeline": [],
  "created_at": "2024-01-15T15:00:00Z"
}
```

### Severity Calculation

```
combined = (anomaly_score * 0.6) + (max_mitre_confidence * 0.4)

>= 0.8: critical
>= 0.6: high
>= 0.4: medium
<  0.4: low
```

## Backend Tools — Findings (11 tools)

These are the actual tools available to Claude via Agent SDK function calling (defined in `backend/schemas/tool_schemas.py`).

### `list_findings`

```json
{
  "severity": "high",
  "data_source": "sysmon",
  "status": "new",
  "sort_by": "timestamp",
  "sort_order": "desc",
  "offset": 0,
  "limit": 20
}
```

### `search_findings`

Full-text search across finding IDs, descriptions, and entity context.

```json
{
  "query": "mimikatz lateral movement",
  "severity": "high",
  "limit": 20
}
```

### `get_findings_stats`

Returns aggregate counts by severity, data source, status, and top MITRE techniques. No parameters required.

### `get_finding`

```json
{ "finding_id": "f-20260209-001" }
```

### `nearest_neighbors`

Find similar findings by embedding similarity.

```json
{
  "finding_id": "f-20260209-001",
  "limit": 10
}
```

### `list_cases`

```json
{
  "status": "in_progress",
  "severity": "high",
  "limit": 50
}
```

### `get_case`

```json
{ "case_id": "case-xxx" }
```

Returns full case details including findings, timeline, activities, and MITRE techniques.

### `create_case`

```json
{
  "title": "Investigation title",
  "description": "Case description",
  "severity": "high",
  "finding_ids": ["f-xxx", "f-yyy"]
}
```

### `add_finding_to_case`

```json
{
  "case_id": "case-xxx",
  "finding_id": "f-xxx"
}
```

### `update_case`

```json
{
  "case_id": "case-xxx",
  "title": "Updated title",
  "status": "resolved",
  "priority": "critical"
}
```

Status values: `open`, `investigating`, `resolved`, `closed`

### `add_resolution_step`

Document a containment, eradication, or recovery action.

```json
{
  "case_id": "case-xxx",
  "description": "What needs to be done",
  "action_taken": "Specific action taken or recommended",
  "result": "Outcome or expected outcome"
}
```

## Backend Tools — Detection Rules (5 tools)

Access to 7,200+ rules across Sigma, Splunk ESCU, Elastic, and KQL formats.

### `analyze_coverage`

```json
{ "techniques": ["T1059.001", "T1071.001"] }
```

### `search_detections`

```json
{
  "query": "powershell base64",
  "source_type": "sigma",
  "limit": 20
}
```

`source_type`: `sigma`, `splunk`, `elastic`, `kql` (optional, searches all if omitted)

### `identify_gaps`

```json
{ "context": "ransomware" }
```

### `get_coverage_stats`

```json
{ "source_type": "splunk" }
```

### `get_detection_count`

```json
{ "source_type": "elastic" }
```

## Backend Tools — MITRE ATT&CK (2 tools)

### `get_attack_layer`

Returns ATT&CK Navigator layer JSON.

```json
{ "layer_type": "coverage" }
```

`layer_type`: `coverage`, `findings`, `detections`

### `get_technique_rollup`

```json
{ "tactic": "initial-access" }
```

## Backend Tools — Approvals (5 tools)

### `list_pending_approvals`

```json
{ "limit": 50 }
```

### `get_approval_action`

```json
{ "action_id": "action-20260209-001" }
```

### `approve_action`

```json
{
  "action_id": "action-xxx",
  "approved_by": "analyst_name"
}
```

### `reject_action`

```json
{
  "action_id": "action-xxx",
  "reason": "False positive confirmed",
  "rejected_by": "analyst_name"
}
```

### `get_approval_stats`

No parameters. Returns total, pending, approved, rejected, and executed counts.

## REST API Endpoints

The FastAPI backend exposes REST endpoints for direct use. Full interactive documentation is available at `http://localhost:6987/docs`.

Key endpoint groups:
- `/api/findings` — Finding CRUD and search
- `/api/cases` — Case management
- `/api/claude` — Chat interface and tool invocation
- `/api/attack` — MITRE ATT&CK layers
- `/api/ai` — AI decision approvals
- `/api/skills` — Workflow execution
- `/api/mcp` — MCP server management
- `/api/ingest` — Data ingestion

## Error Format

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Finding not found"
  }
}
```

| Code | Description |
|------|-------------|
| `NOT_FOUND` | Resource not found |
| `INVALID_PARAMETER` | Invalid parameter |
| `ACCESS_DENIED` | Insufficient permissions |
| `RATE_LIMITED` | Too many requests |

## Entity Context by Data Source

### Flow

```json
{
  "src_ip": "10.0.1.15",
  "dst_ip": "203.0.113.50",
  "src_port": 54321,
  "dst_port": 443,
  "protocol": "tcp",
  "bytes_sent": 1024,
  "hostname": "workstation-042"
}
```

### DNS

```json
{
  "src_ip": "10.0.1.15",
  "query_name": "suspicious.com",
  "query_type": "A",
  "response_ips": ["203.0.113.50"]
}
```

### WAF

```json
{
  "src_ip": "203.0.113.100",
  "method": "POST",
  "uri": "/api/upload",
  "rule_matched": "942100",
  "rule_category": "SQL Injection"
}
```
