# Integrations

Configure integrations via **Settings > Integrations** in the web UI.

## Backend Tools via Agent SDK (Recommended)

**NEW**: Backend tool integration via Claude Agent SDK eliminates desktop dependency.

### Available Tools (19)

| Category | Count | Tools |
|----------|-------|-------|
| **Security Detections** | 5 | Coverage analysis, detection search, gap identification |
| **Findings & Cases** | 7 | List/get findings, case management, similarity search |
| **MITRE ATT&CK** | 2 | Attack layer generation, technique rollup |
| **Approval Workflow** | 5 | Pending approvals, approve/reject actions, statistics |

### Benefits

- ✅ **Zero Desktop Dependency** - Works entirely through Agent SDK
- ✅ **Web UI Compatible** - All tools accessible via browser
- ✅ **Production Ready** - Multi-user deployments
- ✅ **Lower Latency** - Direct function calls via Agent SDK
- ✅ **Simpler Deployment** - No MCP server configuration needed

### Usage

Backend tools are automatically enabled for web UI users via the Claude Agent SDK. See [Backend Tools Guide](BACKEND_TOOLS.md) for detailed documentation.

## MCP Servers (Optional - Advanced Integration)

> **Note**: MCP servers are optional and primarily used for advanced integrations requiring external services (Splunk, VirusTotal, etc.). Web UI users get full functionality through the Agent SDK backend tools.

| Category | Servers | Status |
|----------|---------|--------|
| Core | deeptempo-findings, approval, attack-layer, tempo-flow | Implemented |
| Community | GitHub, PostgreSQL | Active |
| Detection Engineering | Security-Detections-MCP | Implemented |
| SIEM | Splunk, Elastic Security | Implemented |
| Timeline | Timesketch | Implemented |
| Threat Intel | VirusTotal, Shodan, AlienVault OTX, MISP, URL Analysis, IP Geolocation | Implemented |
| EDR | CrowdStrike | Implemented |
| Sandbox | Hybrid Analysis, Joe Sandbox, ANY.RUN, CAPE Sandbox | Implemented |
| Ticketing | Jira | Implemented |
| Communication | Slack | Implemented |
| Data Pipeline | Cribl Stream | Implemented |

## Security-Detections-MCP

Detection engineering with 7,200+ rules across Sigma, Splunk ESCU, Elastic, and KQL formats.

### Overview

Security-Detections-MCP provides:
- **7,200+ Detection Rules** - Comprehensive detection rule database
- **71+ Tools** - Coverage analysis, gap identification, template generation
- **11 Expert Prompts** - Guided detection engineering workflows
- **Tribal Knowledge** - Document and retrieve detection decisions
- **Pattern Intelligence** - Learn from existing detection rules

### Configuration

Automatically configured during `./setup_dev.sh`. Detection repositories are cloned to `~/security-detections/`.

To skip automatic installation:
```bash
SKIP_DETECTION_REPOS=true ./setup_dev.sh
```

To update repositories:
```bash
./scripts/setup_detection_repos.sh --update
```

### Key Tool Categories

| Category | Tools | Description |
|----------|-------|-------------|
| Coverage Analysis | 6 tools | Quantify detection coverage by MITRE technique |
| Detection Search | 12 tools | Find relevant detection rules |
| Pattern Intelligence | 15 tools | Learn from existing detection patterns |
| Template Generation | 8 tools | AI-assisted detection rule creation |
| Tribal Knowledge | 20 tools | Document and retrieve detection decisions |
| Analytics & Reporting | 10 tools | Metrics and gap analysis reports |

### Expert Workflow Prompts

11 guided workflows for detection engineering:
- `apt-threat-emulation` - Purple team exercises for APT groups
- `coverage-analysis` - Comprehensive coverage assessment
- `detection-tuning` - Optimize existing detections
- `gap-prioritization` - Prioritize detection gaps
- `mitre-mapping` - Map findings to ATT&CK framework
- `purple-team-report` - Generate purple team reports
- `threat-landscape-sync` - Align to current threat landscape
- `detection-validation` - Validate detection effectiveness
- `sigma-to-platform` - Convert Sigma to platform-specific
- `coverage-heatmap` - Visualize detection coverage
- `detection-lifecycle` - Manage detection lifecycle

### Primary Use Cases

**For MITRE Analyst Agent:**
```
"What's our detection coverage for APT29?"
"What detection gaps exist for ransomware?"
"Generate a Splunk detection for T1059.001 PowerShell execution"
```

**For Threat Hunter Agent:**
```
"What patterns exist for detecting C2 beaconing?"
"Extract common fields used in PowerShell detections"
"Show me similar detections to the one we just created"
```

**For Investigator Agent:**
```
"Would our current detections catch this attack?"
"What detections exist for technique T1071.001?"
"Document why we prioritized this detection"
```

### Documentation

See [DETECTION_ENGINEERING.md](DETECTION_ENGINEERING.md) for complete usage guide, examples, and best practices.

### Verification

Test integration:
```bash
python scripts/test_detection_integration.py
```

## Splunk

Natural language to SPL query generation.

### Configuration

```bash
SPLUNK_PASSWORD="your_password"
```

Settings > Integrations > Splunk:
- Server URL: `https://splunk.example.com:8089`
- Username
- Verify SSL

### MCP Tools

| Tool | Description |
|------|-------------|
| `generate_spl_query` | Natural language to SPL |
| `execute_spl_search` | Run SPL query |
| `search_by_ip` | Quick IP search |
| `search_by_hostname` | Quick hostname search |
| `search_by_username` | Quick user search |
| `natural_language_search` | Generate and execute |
| `get_splunk_indexes` | List available indexes |

## Elastic Security

Elasticsearch SIEM with detection alert ingestion, bi-directional case sync, and IOC enrichment.

### Configuration

```bash
ELASTIC_HOST="https://elasticsearch.example.com:9200"
ELASTIC_KIBANA_URL="https://kibana.example.com:5601"
ELASTIC_API_KEY="your_api_key"
# Or use basic auth:
# ELASTIC_USERNAME="elastic"
# ELASTIC_PASSWORD="your_password"
```

Settings > Integrations > Elastic Security (SIEM):
- Elasticsearch URL
- Kibana URL (required for detection alerts and case sync)
- API Key or Username/Password
- Alert Index Pattern (default: `.alerts-security.alerts-default`)

### MCP Tools

| Tool | Description |
|------|-------------|
| `elastic_search_logs` | Search Elasticsearch with query DSL |
| `elastic_search_by_ioc` | Search by IOC (IP, hash, username, hostname) |
| `elastic_get_indices` | List available indices |
| `elastic_get_detection_alerts` | Fetch recent detection alerts |

### Features

- **Alert Ingestion**: Daemon poller fetches detection alerts from Kibana Detections API
- **Bi-directional Sync**: Case status changes in Vigil sync back to Elastic Security alerts
- **IOC Enrichment**: Agents query Elasticsearch indices for logs matching case IOCs

## Timesketch

Forensic timeline analysis.

### Configuration

Settings > Integrations > Timesketch:
- Server URL: `http://localhost:5000` (local) or production URL
- Auth: Username/Password or API Token
- Auto-sync interval (optional)

### MCP Tools

| Tool | Description |
|------|-------------|
| `list_sketches` | List investigation workspaces |
| `get_sketch` | Get sketch details |
| `create_sketch` | Create new workspace |
| `search_timesketch` | Lucene query search |
| `export_to_timesketch` | Export findings/cases |

## Threat Intelligence

### VirusTotal

```bash
# Settings > Integrations > VirusTotal
VT_API_KEY="your_api_key"
```

Tools: `vt_check_hash`, `vt_check_ip`, `vt_check_domain`, `vt_check_url`

### Shodan

```bash
SHODAN_API_KEY="your_api_key"
```

Tools: `shodan_search_ip`, `shodan_get_host_info`, `shodan_search_exploits`

### AlienVault OTX

```bash
OTX_API_KEY="your_api_key"
```

Tools: `otx_get_indicator`, `otx_search_pulses`, `otx_get_pulse`

### MISP

```bash
MISP_URL="https://misp.example.com"
MISP_API_KEY="your_api_key"
```

Tools: `misp_search`, `misp_get_event`, `misp_add_attribute`

### Cloudflare Cloudforce One (STIX/TAXII)

Pulls Cloudflare's Cloudforce One threat feed via TAXII 2.1 into the
`threat_indicators` table. The daemon polls on the configured interval; the
finding processor enriches IOCs against this table and surfaces matches
under `finding.enrichment.threat_indicators`.

```bash
# Settings → Integrations → Cloudflare Cloudforce One
# (env vars are fallbacks; the integration is the on/off switch)
CLOUDFORCE_ONE_API_TOKEN="..."
CLOUDFORCE_ONE_TAXII_SERVER_URL="https://api.cloudflare.com/client/v4/accounts/{account_id}/cloudforce-one/threat-events/taxii2/"
CLOUDFORCE_ONE_COLLECTION_IDS="collection-uuid-1,collection-uuid-2"
THREAT_FEED_POLL_INTERVAL="900"
```

Operates independently of the Cloudflare WAF/Zero Trust integration —
customers can subscribe to either or both.

## Sandbox Analysis

### Hybrid Analysis

```bash
HYBRID_ANALYSIS_API_KEY="your_api_key"
```

Tools: `ha_submit_file`, `ha_get_report`, `ha_search`

### Joe Sandbox

```bash
JOE_SANDBOX_API_KEY="your_api_key"
```

Tools: `joe_submit`, `joe_get_report`, `joe_search`

### ANY.RUN

```bash
ANYRUN_API_KEY="your_api_key"
```

Tools: `anyrun_get_report`, `anyrun_search`, `anyrun_get_iocs`

### CAPE Sandbox

Open-source Cuckoo fork for on-prem detonation. Vigil ships an MCP client
(`tools/cape_sandbox.py`) that talks to an existing CAPE deployment over
its REST API — Vigil does **not** host CAPE itself. CAPE requires KVM and
Windows guest VMs, so it's typically deployed on bare metal, not inside
Docker Desktop.

```bash
CAPE_SANDBOX_ENABLED="true"
CAPE_SANDBOX_URL="http://cape.internal:8000"
CAPE_SANDBOX_API_KEY="your_cape_api_token"
```

Tools: `cape_submit_file`, `cape_submit_url`, `cape_get_report`,
`cape_get_iocs`, `cape_get_pcap`, `cape_list_tasks`, `cape_search_hash`,
`cape_task_status`.

### Auto-submission pipeline

When `SANDBOX_AUTO_SUBMIT=true` and at least one sandbox is enabled, the
daemon will, during finding enrichment, consult each sandbox's hash cache
for any file hash on the finding. Safety gates:

- File extension must be in `SANDBOX_ALLOWED_FILE_TYPES` (default list
  matches common malware extensions).
- `file_size` (when known) must be ≤ `SANDBOX_MAX_FILE_SIZE_MB`.
- No binary bytes are ever sent from Vigil. Only hash-cache lookups and
  sandbox-API submission-by-hash are performed.

A companion scheduler task (`sandbox_poll`, default every 60s) picks up
completed reports and writes them into the case as `CaseEvidence` plus
extracted IOCs into `CaseIOC`. See [SANDBOX.md](./SANDBOX.md).

## Network Security & Edge Enforcement

### Cloudflare (WAF, Zero Trust Gateway, Access)

Closes the loop by letting Vigil propose and (with approval) execute
enforcement actions on Cloudflare's edge:

| Action type | MCP tool | Cloudflare API |
|---|---|---|
| `WAF_BLOCK` | `cf_waf_block_ip` / `cf_waf_unblock_ip` | IP Access Rules (`/firewall/access_rules/rules`) |
| `GATEWAY_BLOCK` | `cf_gateway_block_domain` | Zero Trust Gateway DNS+HTTP rule (`/gateway/rules`) |
| `ACCESS_REVOKE` | `cf_access_revoke_session` | Access organization revoke (`/access/organizations/revoke_user`) |

Read-only context tools — `cf_lookup_ip_threat` and `cf_lookup_domain_threat`
— are wired into the Threat Intel and Network Analyst agents so investigations
can pull edge context on demand.

```bash
# Settings → Integrations → Cloudflare
# Required scopes: Zone:Firewall Services:Edit, Account:Zero Trust:Edit,
#                  Account:Access:Edit, Account:Account Analytics:Read
CLOUDFLARE_API_TOKEN="..."
CLOUDFLARE_ACCOUNT_ID="..."   # required for Zero Trust + Access actions
CLOUDFLARE_ZONE_ID="..."      # optional default zone for WAF rules
```

All write actions route through `services/approval_service.py`; the auto-
responder agent only auto-approves at confidence ≥ 0.90, otherwise an analyst
must approve in the Approvals UI before the daemon executes the call.

### Cloudflare Cloudy ingestion (gated)

Inbound webhook receiver at `POST /api/webhooks/cloudflare/cloudy` for
Cloudflare-pushed events with attached Cloudy natural-language summaries.
**Off by default** because the upstream contract is not publicly stable
yet; the router only mounts when `CLOUDY_INGESTION_ENABLED=true` and a
`CLOUDY_WEBHOOK_SECRET` is set for HMAC-SHA256 verification.

When enabled, Cloudy summaries land on findings as
`finding.evidence.cloudy_summary` (cited verbatim, with provenance) and
are surfaced into the Threat Intel agent's context window.

### CrowdStrike

```bash
CS_CLIENT_ID="your_client_id"
CS_CLIENT_SECRET="your_client_secret"
```

Tools: `get_crowdstrike_alert_by_ip`, `crowdstrike_foundry_isolate`, `crowdstrike_foundry_unisolate`, `get_host_status`

## Communication

### Slack

```bash
SLACK_BOT_TOKEN="xoxb-..."
SLACK_DEFAULT_CHANNEL="#soc-alerts"
```

Tools: `slack_send_message`, `slack_send_alert`, `slack_create_channel`, `slack_upload_file`

### Jira

```bash
JIRA_URL="https://company.atlassian.net"
JIRA_EMAIL="user@company.com"
JIRA_API_TOKEN="your_token"
```

Tools: `jira_create_issue`, `jira_update_issue`, `jira_add_comment`, `jira_search`, `jira_get_issue`

## Data Pipeline

### Cribl Stream

```bash
CRIBL_PASSWORD="your_password"
CRIBL_WORKER_GROUP="default"
```

Benefits:
- Normalize log formats before DeepTempo analysis
- Filter noise, reduce Splunk ingestion 30-50%
- Enrich events with GeoIP, asset info
- Route data to multiple destinations

```
Data Sources -> Cribl Stream -> DeepTempo LogLM
                            -> Splunk
                            -> S3/Data Lake
```

## Adding Custom Integrations

Settings > Integrations > Custom Integration Builder:

1. Upload API documentation
2. AI generates MCP server code
3. Review and test
4. Deploy to `tools/` directory

## CloudCurrent VStrike (Network Topology Fusion)

VStrike enriches DeepTempo findings with network topology, asset, segment,
and mission-system context, then pushes the enriched findings back into
Vigil. Vigil can also query VStrike on demand for asset topology and blast
radius.

### Integration surface

| Direction | Endpoint / Tool | Purpose |
|-----------|-----------------|---------|
| VStrike → Vigil | `POST /api/integrations/vstrike/findings` | Push enriched findings (batched) |
| Vigil → VStrike | `GET /api/integrations/vstrike/health` | Outbound reachability check |
| Vigil → VStrike | `GET /api/integrations/vstrike/topology/asset/{id}` | Asset topology lookup |
| Vigil → VStrike | `GET /api/integrations/vstrike/topology/asset/{id}/adjacent` | One-hop neighbors |
| Vigil → VStrike | `GET /api/integrations/vstrike/topology/asset/{id}/blast-radius` | Blast radius |
| Vigil → VStrike | `POST /api/integrations/vstrike/network-graph` | Full network graph: `{label, nodes, edges, bbox}` |
| Vigil → VStrike | `POST /api/integrations/vstrike/ui/legend-apply` | Apply selected legend in the iframe |
| Vigil → VStrike | `POST /api/integrations/vstrike/ui/rightpanel-focus` | Open / focus the iframe's right-hand details panel |
| MCP | `tools/vstrike.py` (`vstrike_*` tools) | Agent-invokable topology queries + UI control |

#### Recently added MCP tools

| Tool | Input | Behavior |
|------|-------|----------|
| `vstrike_network_graph_get` | `networkId` (optional) | Returns `{label, nodes, edges, bbox}` for the active (or specified) network. Useful for blast-radius, path-finding, and layout reasoning beyond single-asset lookups. |
| `vstrike_ui_legend_apply` | `legendId` (required, value from `legend-run-list`), `networkId` (optional) | Applies the selected legend run inside the active VStrike iframe session. |
| `vstrike_ui_rightpanel_focus` | none | Opens the VStrike iframe's right-hand details panel for whatever node is currently selected. |

UX wiring:

- The toolbar's Legend dropdown now has an Apply button parallel to the existing Storyline Apply.
- Picking a node from the toolbar search results chains `ui-camera-node` → `ui-rightpanel-focus` so the camera selects the node and its details panel opens automatically.
- Clicking an adjacent-asset chip in `NetworkContextPanel` chains the same way: Vigil highlights the node in its own EntityGraph, then drives VStrike with `cameraNode` + `focusRightPanel`.

Service methods accept `**kwargs` and REST routes use `extra="allow"` Pydantic models, so future schema bumps on VStrike's side don't require a Vigil refactor.

### Configuration

Either set env vars (recommended for push/CI) or configure via the UI:

```bash
# .env
VSTRIKE_BASE_URL="https://vstrike.example.com"
VSTRIKE_API_KEY="<outbound bearer token>"
VSTRIKE_VERIFY_SSL="true"
VSTRIKE_INBOUND_API_KEY="<bearer token Vigil expects on inbound push>"
```

UI: **Settings → Integrations → CloudCurrent VStrike**.

### Storage model

VStrike enrichment lives at `finding.entity_context["vstrike"]` (JSONB —
no DB migration required). Shape is defined by
`backend/schemas/vstrike.py::VStrikeEnrichment` and mirrored by
`frontend/src/types/vstrike.ts`.

The ingest handler does read-modify-write on `entity_context` so existing
keys (`src_ip`, `hostname`, etc.) are never clobbered.

### Auto-case clustering

When `auto_cluster_cases: true` (default), the ingest handler groups
upserted findings by `(segment, attack_path[0] or asset_id)` and creates
one case per group via
`services.case_automation_service.cluster_findings_by_attack_path`.

### Authentication

Inbound push uses a bearer token:

```
Authorization: Bearer $VSTRIKE_INBOUND_API_KEY
```

When `DEV_MODE=true`, the auth check is bypassed (matches the rest of the
codebase). Outside dev mode, the endpoint returns:
- `401` if the header is missing or wrong
- `503` if `VSTRIKE_INBOUND_API_KEY` is unset (refuses to run open)

### Example push

```bash
export DEV_MODE=true
curl -X POST http://localhost:6987/api/integrations/vstrike/findings \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_id": "demo-1",
    "findings": [{
      "finding_id": "f-test-1",
      "timestamp": "2026-05-20T14:00:00Z",
      "anomaly_score": 0.87,
      "vstrike_enrichment": {
        "asset_id": "srv-01",
        "asset_name": "SAP-PROD-01",
        "segment": "mgmt-vlan-10",
        "site": "JBSA",
        "criticality": "high",
        "mission_system": "C2-AWACS",
        "attack_path": ["ext-gw-01", "dmz-web-02", "srv-01"],
        "blast_radius": 14,
        "adjacent_assets": [
          {"asset_id": "dc-01", "hop_distance": 1, "edge_technique": "T1021.002"}
        ],
        "enriched_at": "2026-05-20T14:00:00Z"
      }
    }]
  }'
```

### Visualization

- **Finding detail**: `NetworkContextPanel` renders the VStrike sub-dict
  (criticality, segment, mission system, blast radius, attack-path
  breadcrumb, clickable adjacent-asset chips).
- **Entity graph**: nodes are tinted by segment when VStrike metadata is
  present; the first MITRE technique on a link is rendered as an edge
  label (always on highlighted links, and at zoom > 2.0 otherwise).
- **Pivot**: clicking an adjacent-asset chip dispatches
  `vstrike-graph-highlight` — `pages/Investigation.tsx` listens for this
  event and feeds the node id into `EntityGraph.highlightedNodes`.

### Testing

```bash
pytest tests/integration/test_vstrike_ingest.py -v
pytest tests/unit/test_vstrike_service.py -v
```

## Stub Servers (Not Implemented)

Available for future implementation:

| Server | Category |
|--------|----------|
| AWS Security Hub | Cloud |
| Azure Sentinel | Cloud |
| GCP Security | Cloud |
| Azure AD | Identity |
| Okta | Identity |
| Microsoft Defender | EDR |
| SentinelOne | EDR |
| Carbon Black | EDR |
| PagerDuty | Communication |
