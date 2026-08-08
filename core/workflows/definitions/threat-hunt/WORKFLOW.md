---
name: threat-hunt
description: "Proactive, hypothesis-driven threat hunting across all available data sources with network analysis, malware examination, and intelligence enrichment."
agents:
  - threat_hunter
  - network_analyst
  - malware_analyst
  - threat_intel
  - reporter
tools-used:
  - list_findings
  - get_finding
  - nearest_neighbors
  - search_detections
  - create_attack_layer
  - get_case
use-case: "Proactive threat hunting -- start with a hypothesis or IOC and systematically search for evidence across network, endpoint, and threat intel sources."
trigger-examples:
  - "Hunt for C2 beaconing activity across all network findings"
  - "Proactive hunt: look for lateral movement via RDP"
  - "Validate whether this DeepTempo C2 alert is real by checking all available threat intel for the public IP"
  - "Hunt for APT28 credential harvesting techniques"
  - "Search for signs of data exfiltration in the last 24 hours"
---

# Threat Hunt Workflow

Proactive, hypothesis-driven threat hunting workflow. Sequences five specialized agents to formulate a hunt hypothesis, analyze network traffic, examine suspicious artifacts, enrich IOCs across threat intel sources, and produce an actionable hunt report.

## When to Use

- Proactive hunting for threats that haven't triggered alerts
- Validating a flagged alert (e.g., DeepTempo C2 detection) against all available sources
- Hypothesis-driven hunting based on specific TTPs or threat actors
- Searching for indicators of compromise across the environment
- Periodic threat hunting exercises

## Agent Sequence

### Phase 1: Hypothesis & Hunt (Threat Hunter Agent)

**Purpose:** Formulate hunt hypothesis based on TTPs, define scope, execute hunt queries, identify anomalies and outliers.

**Tools:** `list_findings`, `nearest_neighbors`, `search_detections`, pattern intelligence tools

**Steps:**
1. Formulate or refine the hunt hypothesis based on input (TTP, IOC, threat actor, or behavior)
2. Define hunt parameters: scope, timeframe, data sources to query
3. Execute hunt queries using available tools:
   - `list_findings` with filters for relevant time range and severity
   - `nearest_neighbors` to find similar patterns via embeddings
   - `search_detections` to check for matching detection rules
4. Identify anomalies, outliers, and suspicious patterns
5. Validate initial findings -- eliminate obvious false positives
6. Document candidate findings requiring deeper analysis

**Output:** Hunt hypothesis document, initial IOCs/anomalies, suspicious patterns, candidate findings for downstream analysis

### Phase 2: Network Analysis (Network Analyst Agent)

**Purpose:** Deep-dive network traffic analysis on suspicious entities from the hunt -- flow patterns, protocol anomalies, C2 beaconing, lateral movement.

**Tools:** `list_findings`, `get_finding`, `search_detections`

**Steps:**
1. Analyze network findings related to suspicious IPs/hosts from Phase 1
2. Examine flow patterns: volumes, destinations, timing
3. Protocol-specific analysis: HTTP, DNS, SMB, RDP, SSH anomalies
4. Detect C2 beaconing: regular intervals, known C2 infrastructure, encoded channels
5. Identify lateral movement: internal-to-internal connections, port scanning, credential reuse
6. Geolocation analysis: connections to anomalous countries or ASNs
7. Establish traffic baselines to highlight deviations

**Output:** Network IOCs (IPs, domains, ports), C2 indicators, lateral movement paths, protocol anomalies, traffic baselines

### Phase 3: Artifact Analysis (Malware Analyst Agent)

**Purpose:** Analyze suspicious binaries and artifacts discovered during the hunt -- static/dynamic analysis, family classification, capability assessment.

**Tools:** `get_finding`, sandbox MCP tools (if available)

**Steps:**
1. Collect suspicious file hashes and artifacts from Phases 1-2
2. Static analysis: file properties, string extraction, import tables, PE structure
3. Dynamic analysis: sandbox execution results (if available via MCP tools)
4. Assess malware capabilities: data theft, backdoor, RAT, ransomware, cryptominer
5. Classify malware family and variant
6. Extract behavioral IOCs: mutex names, registry keys, file paths, network callbacks
7. Identify C2 infrastructure embedded in binaries
8. Generate detection signatures (YARA, Sigma)

**Output:** Malware family classification, behavioral analysis, extracted IOCs, C2 infrastructure, detection signatures

### Phase 4: Intelligence Enrichment (Threat Intel Agent)

**Purpose:** Enrich all discovered IOCs across multiple threat intelligence sources. Attribute to threat actors, track campaigns.

**Tools:** `get_finding`, `list_findings`, threat intel MCP tools (VirusTotal, Shodan, AlienVault OTX)

**Steps:**
1. Compile all IOCs from Phases 1-3 (IPs, domains, hashes, URLs)
2. Enrich each IOC across available sources:
   - IP/domain reputation and geolocation
   - Hash lookups in malware databases
   - Shodan data for exposed services
   - AlienVault OTX pulse matching
3. Identify threat actor attribution (with stated confidence level)
4. Track campaign patterns: shared infrastructure, similar TTPs, targeting
5. Assess threat context: actor motivations, objectives, typical targets
6. Produce actionable intelligence: blocking recommendations, additional IOCs to hunt

**Output:** Enriched IOC profiles, threat actor attribution with confidence, campaign tracking, actionable intelligence for blocking/hunting

### Phase 5: Hunt Report (Reporter Agent)

**Purpose:** Consolidate all hunt results into an actionable report with hypothesis validation, IOC summary, and detection recommendations.

**Tools:** `get_case`, `list_findings`, `create_attack_layer`

**Steps:**
1. Compile results from all hunt phases
2. Validate or refute the original hypothesis with evidence
3. Generate MITRE ATT&CK Navigator layer for discovered techniques
4. Structure the hunt report:
   - **Hunt Summary:** Hypothesis, scope, methodology
   - **Hypothesis Validation:** Confirmed, refuted, or inconclusive -- with evidence
   - **Findings:** All anomalies and threats discovered
   - **IOC Inventory:** Complete list with enrichment data
   - **MITRE ATT&CK Mapping:** Techniques observed
   - **Detection Recommendations:** New rules to add, gaps to close
   - **Executive Brief:** High-level summary for leadership

**Output:** Hunt summary report, validated/refuted hypothesis, complete IOC list, ATT&CK layer, detection recommendations

## Example Invocation

```
User: "Validate whether this DeepTempo C2 alert is real by checking all threat intel for IP 185.220.101.1"
```

## Expected Output

```json
{
  "workflow": "threat-hunt",
  "phases_completed": ["hypothesis-hunt", "network-analysis", "artifact-analysis", "intel-enrichment", "report"],
  "hypothesis": "Suspected C2 communication with 185.220.101.1",
  "hypothesis_status": "confirmed",
  "iocs_discovered": {
    "ips": ["185.220.101.1", "185.220.101.5"],
    "domains": ["update-service.example.com"],
    "hashes": ["a1b2c3..."]
  },
  "threat_actor": {
    "name": "APT28",
    "confidence": 0.72,
    "ttps": ["T1071.001", "T1059.001", "T1078"]
  },
  "beaconing_detected": true,
  "beaconing_interval": "300s",
  "detection_recommendations": [
    "Add Sigma rule for DNS queries to update-service.example.com",
    "Block IP range 185.220.101.0/24 at perimeter"
  ]
}
```
