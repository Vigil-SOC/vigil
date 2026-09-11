---
name: forensic-analysis
description: "Post-incident digital forensics with evidence preservation, malware deep-dive, network forensics, and audit-ready documentation suitable for legal proceedings."
use_case: "Post-incident forensic examination of artifacts, timeline reconstruction with chain-of-custody documentation, suitable for legal proceedings or compliance audits."
trigger_examples:
  - "Conduct forensic analysis on the compromised host findings"
  - "Post-incident forensics for case CASE-20260215-breach"
  - "Forensic examination of these artifacts for legal hold"
  - "Run forensics workflow on this data breach incident"
objectives:
  - "Acquire evidence without modifying originals and record its custody"
  - "Characterise the malware and the network activity it produced"
  - "Reconstruct a master timeline the report can rest on"
  - "Produce documentation that survives legal and compliance scrutiny"
phases:
  - id: evidence-acquisition
    agent: forensics
    name: "Evidence Acquisition & Preservation"
    tools: [get_finding, list_findings, recall_entity]
    instructions: |
      Acquire all evidence via tools without modifying originals, establish chain
      of custody, create an artifact inventory and reconstruct a master timeline.

      1. Retrieve all findings related to the incident via `get_finding` and
         `list_findings`
      2. Establish chain-of-custody documentation:
         - Record when each piece of evidence was accessed
         - Document the source and retrieval method
         - Note the state of the evidence at the time of acquisition
      3. Build an artifact inventory: all files, logs, memory captures and
         network captures referenced
      4. Reconstruct a master timeline from all available timestamps
      5. Extract initial IOCs: file hashes, IPs, domains, file paths, registry keys
      6. Identify artifacts requiring deeper analysis (suspicious binaries,
         encrypted files, anomalous logs)

      The chain of custody is the product here, not a formality: an artifact you
      examined but did not record the handling of cannot be relied on later.
      Hand on the custody log, artifact inventory, master timeline, IOCs and the
      list of artifacts for deeper analysis.

  - id: malware-analysis
    agent: malware_analyst
    name: "Malware & Artifact Deep-Dive"
    tools: [get_finding, recall_entity]
    instructions: |
      Static and dynamic analysis of the suspicious artifacts: PE structure,
      string extraction, sandbox results, capability assessment and family
      classification.

      1. Take the suspicious artifacts and hashes from the previous step
      2. Static analysis:
         - File properties (size, type, timestamps, metadata)
         - String extraction (URLs, IPs, commands, paths)
         - Import table analysis (suspicious API calls)
         - PE structure examination (sections, entropy, packing)
      3. Dynamic analysis, where sandbox tools are available:
         - Sandbox execution behaviour
         - Process creation and injection
         - File system and registry modifications
         - Network communications
      4. Assess capabilities: data theft, backdoor, RAT, ransomware, keylogger,
         cryptominer
      5. Classify the malware family and variant
      6. Extract behavioural IOCs: mutex names, registry keys, scheduled tasks,
         persistence mechanisms
      7. Identify C2 infrastructure from the binary analysis
      8. Generate detection signatures: YARA rules, Sigma rules

      Where a sandbox was unavailable, say so rather than reporting static
      analysis as though it were the whole picture.

  - id: network-forensics
    agent: network_analyst
    name: "Network Forensics"
    tools: [list_findings, get_finding, search_detections, recall_entity]
    instructions: |
      Reconstruct communication timelines, identify data exfiltration, map
      lateral movement and identify external C2.

      1. Analyse all network-related findings from the incident
      2. Reconstruct the complete communication timeline:
         - When did external communications begin?
         - What was the sequence of internal lateral movement?
         - When did data exfiltration occur, if it did?
      3. Assess data exfiltration:
         - Volume of data transferred externally
         - Destination analysis (IPs, domains, cloud services)
         - Method (HTTP, DNS tunnelling, encrypted channels)
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

      Quantify. "Every 300s +/- 4s over 6 hours, 412 connections" is a finding;
      "frequent connections" is not.

  - id: report
    agent: reporter
    name: "Forensic Report"
    tools: [get_case, list_findings, create_attack_layer, recall_entity]
    instructions: |
      Produce an audit-ready forensic report with executive summary, technical
      findings, evidence chain and legal-grade documentation.

      1. Compile all step outputs, maintaining chain-of-custody integrity
      2. Generate a MITRE ATT&CK Navigator layer for every technique identified
      3. Structure the report:
         - **Executive Summary:** incident overview, impact and risk in plain language
         - **Chain of Custody:** complete evidence handling documentation
         - **Evidence Inventory:** all artifacts examined, with metadata
         - **Master Timeline:** chronological reconstruction of all events
         - **Malware Analysis:** detailed artifact examination results
         - **Network Forensics:** communication analysis and exfiltration assessment
         - **MITRE ATT&CK Mapping:** techniques and tactics visualisation
         - **IOC Appendix:** hashes, IPs, domains, file paths, registry keys
         - **Impact Assessment:** what was compromised, what data was at risk
         - **Remediation Recommendations:** steps to prevent recurrence
         - **Compliance Impact:** notification requirements (GDPR, HIPAA, PCI-DSS)

      This report may be read in a legal proceeding. Distinguish what was
      observed from what was inferred, and name anything the earlier steps could
      not examine rather than leaving the absence to be read as an absence of
      activity.
---

# Forensic Analysis Workflow

Post-incident digital forensics workflow with emphasis on evidence preservation, chain-of-custody documentation, and legal-grade reporting. Sequences four specialized agents to acquire evidence, analyze malware artifacts, reconstruct network communications, and produce audit-ready documentation.

## When to Use

- Post-incident forensic investigation
- Evidence needs to be preserved for legal proceedings
- Compliance audit requires detailed artifact analysis
- A breach has been confirmed and needs thorough forensic examination
- Chain-of-custody documentation is required

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
