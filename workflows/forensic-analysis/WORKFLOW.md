---
name: forensic-analysis
description: "Post-incident digital forensics with evidence preservation, malware deep-dive, network forensics, and audit-ready documentation suitable for legal proceedings."
agents:
  - forensics
  - malware_analyst
  - network_analyst
  - reporter
tools-used:
  - get_finding
  - list_findings
  - nearest_neighbors
  - search_detections
  - get_case
  - create_attack_layer
use-case: "Post-incident forensic examination of artifacts, timeline reconstruction with chain-of-custody documentation, suitable for legal proceedings or compliance audits."
trigger-examples:
  - "Conduct forensic analysis on the compromised host findings"
  - "Post-incident forensics for case CASE-20260215-breach"
  - "Forensic examination of these artifacts for legal hold"
  - "Run forensics workflow on this data breach incident"
---

# Forensic Analysis Workflow

Post-incident digital forensics workflow with emphasis on evidence preservation, chain-of-custody documentation, and legal-grade reporting. Sequences four specialized agents to acquire evidence, analyze malware artifacts, reconstruct network communications, and produce audit-ready documentation.

## When to Use

- Post-incident forensic investigation
- Evidence needs to be preserved for legal proceedings
- Compliance audit requires detailed artifact analysis
- A breach has been confirmed and needs thorough forensic examination
- Chain-of-custody documentation is required

## Agent Sequence

### Phase 1: Evidence Acquisition & Preservation (Forensics Agent)

**Purpose:** Acquire all evidence via tools without modifying originals, establish chain of custody, create artifact inventory, reconstruct master timeline.

**Tools:** `get_finding`, `list_findings`, `nearest_neighbors`

**Steps:**
1. Retrieve all findings related to the incident via `get_finding` and `list_findings`
2. Establish chain of custody documentation:
   - Record when each piece of evidence was accessed
   - Document the source and retrieval method
   - Note the state of evidence at time of acquisition
3. Build artifact inventory: all files, logs, memory captures, network captures referenced
4. Use `nearest_neighbors` to discover related findings via embedding similarity
5. Reconstruct master timeline from all available timestamps across findings
6. Extract initial IOCs from forensic artifacts: file hashes, IPs, domains, file paths, registry keys
7. Identify artifacts requiring deeper analysis (suspicious binaries, encrypted files, anomalous logs)

**Output:** Chain of custody log, artifact inventory, master timeline, IOCs from forensic artifacts, list of artifacts for deeper analysis

### Phase 2: Malware & Artifact Deep-Dive (Malware Analyst Agent)

**Purpose:** Static and dynamic analysis of suspicious artifacts -- PE structure, string extraction, sandbox results, capability assessment, family classification.

**Tools:** `get_finding`, sandbox MCP tools (if available)

**Steps:**
1. Receive suspicious artifacts/hashes from Phase 1
2. Static analysis:
   - File properties (size, type, timestamps, metadata)
   - String extraction (URLs, IPs, commands, paths)
   - Import table analysis (suspicious API calls)
   - PE structure examination (sections, entropy, packing)
3. Dynamic analysis (if sandbox tools available):
   - Sandbox execution behavior
   - Process creation and injection
   - File system modifications
   - Registry modifications
   - Network communications
4. Assess capabilities: data theft, backdoor, RAT, ransomware, keylogger, cryptominer
5. Classify malware family and variant
6. Extract behavioral IOCs: mutex names, registry keys, scheduled tasks, persistence mechanisms
7. Identify C2 infrastructure from binary analysis
8. Generate detection signatures: YARA rules, Sigma rules

**Output:** Malware capabilities, family/variant ID, C2 infrastructure, behavioral IOCs, persistence mechanisms, detection signatures

### Phase 3: Network Forensics (Network Analyst Agent)

**Purpose:** Forensic network analysis -- reconstruct communication timelines, identify data exfiltration, map lateral movement, identify external C2.

**Tools:** `list_findings`, `get_finding`, `search_detections`

**Steps:**
1. Analyze all network-related findings from the incident
2. Reconstruct complete communication timeline:
   - When did external communications begin?
   - What was the sequence of internal lateral movement?
   - When did data exfiltration occur (if applicable)?
3. Data exfiltration assessment:
   - Volume of data transferred externally
   - Destination analysis (IPs, domains, cloud services)
   - Method (HTTP, DNS tunneling, encrypted channels)
4. Map lateral movement:
   - Host-to-host communication patterns
   - Credential reuse across systems
   - Protocol usage (SMB, RDP, SSH, WMI, PSExec)
5. Identify all external C2 connections:
   - Beaconing patterns and intervals
   - Known C2 infrastructure matching
   - Geolocation and ASN analysis
6. Protocol-level anomaly analysis
7. Extract all network IOCs

**Output:** Communication timeline, data exfiltration assessment, lateral movement map, C2 connections, network IOCs

### Phase 4: Forensic Report (Reporter Agent)

**Purpose:** Audit-ready forensic report with executive summary, technical findings, evidence chain, and legal-grade documentation.

**Tools:** `get_case`, `list_findings`, `create_attack_layer`

**Steps:**
1. Compile all phase outputs maintaining chain-of-custody integrity
2. Generate MITRE ATT&CK Navigator layer for all techniques identified
3. Structure the forensic report:
   - **Executive Summary:** Incident overview, impact, and risk in plain language
   - **Chain of Custody:** Complete evidence handling documentation
   - **Evidence Inventory:** All artifacts examined with metadata
   - **Master Timeline:** Chronological reconstruction of all events
   - **Malware Analysis:** Detailed artifact examination results
   - **Network Forensics:** Communication analysis and exfiltration assessment
   - **MITRE ATT&CK Mapping:** Techniques and tactics visualization
   - **IOC Appendix:** Complete IOC list (hashes, IPs, domains, file paths, registry keys)
   - **Impact Assessment:** What was compromised, what data was at risk
   - **Remediation Recommendations:** Steps to prevent recurrence
   - **Compliance Impact:** Regulatory notification requirements (GDPR, HIPAA, PCI-DSS, etc.)

**Output:** Complete forensic report (executive + technical + legal sections), evidence inventory with chain of custody, master timeline, ATT&CK layer, IOC appendix, compliance impact

## Example Invocation

```
User: "Conduct forensic analysis on the compromised host findings in case CASE-20260215-breach"
```

## Expected Output

```json
{
  "workflow": "forensic-analysis",
  "phases_completed": ["evidence-acquisition", "malware-analysis", "network-forensics", "report"],
  "artifacts_examined": 12,
  "chain_of_custody_entries": 28,
  "malware_found": {
    "family": "Cobalt Strike",
    "capabilities": ["backdoor", "lateral_movement", "data_exfiltration"],
    "c2_servers": ["185.220.101.1:443"]
  },
  "data_exfiltration": {
    "detected": true,
    "volume_estimate": "2.3 GB",
    "destination": "185.220.101.1",
    "method": "HTTPS"
  },
  "lateral_movement": {
    "hosts_affected": ["HOST-42", "HOST-17", "DC-01"],
    "protocols_used": ["SMB", "WMI"]
  },
  "mitre_techniques": ["T1059.001", "T1071.001", "T1021.002", "T1003.001", "T1048"],
  "compliance_notifications_required": ["GDPR_72hr", "state_breach_notification"],
  "report_sections": ["executive_summary", "chain_of_custody", "evidence", "timeline", "malware", "network", "mitre", "iocs", "impact", "remediation", "compliance"]
}
```
