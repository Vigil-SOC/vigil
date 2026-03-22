---
name: incident-response
description: "Respond to active security incidents with rapid triage, deep investigation, containment, and documentation. Follows NIST IR framework."
agents:
  - triage
  - investigator
  - responder
  - reporter
tools-used:
  - get_finding
  - list_findings
  - nearest_neighbors
  - search_detections
  - create_approval_action
  - update_case
  - create_case
  - get_case
  - create_attack_layer
use-case: "Active incident response -- an alert fires and the SOC needs to triage, investigate, contain, and document."
trigger-examples:
  - "Run incident response on finding f-20260215-abc123"
  - "We have an active incident -- ransomware detected on HOST-42"
  - "Respond to this critical alert"
  - "IR workflow for this phishing finding"
---

# Incident Response Workflow

Multi-agent incident response workflow following the NIST Incident Response framework. Sequences four specialized agents to rapidly triage, deeply investigate, contain threats, and produce audit-ready documentation.

## When to Use

- A security alert has fired and needs immediate response
- A finding has been flagged as critical or high severity
- An active threat (malware, C2, data exfiltration) has been detected
- You need end-to-end incident handling from triage to report

## Agent Sequence

### Phase 1: Triage & Classify (Triage Agent)

**Purpose:** Rapid assessment of the alert -- severity scoring, false-positive check, categorization.

**Tools:** `get_finding`, `list_findings`

**Steps:**
1. Fetch the finding via `get_finding` to retrieve full details
2. Assess severity based on anomaly score, data source, and MITRE techniques
3. Categorize the alert: malware, intrusion, policy violation, reconnaissance, exfiltration, or false positive
4. Assign priority: Critical (immediate action), High (within 1hr), Medium (queue), Low (monitor), False Positive (dismiss)
5. Make escalation decision with reasoning

**Output:** Severity score, alert category, priority level, escalation decision

**Short-circuit:** If classified as False Positive, skip to Phase 4 for a brief dismissal report.

### Phase 2: Deep Investigation (Investigator Agent)

**Purpose:** Root cause analysis, evidence collection, timeline reconstruction, cross-source correlation.

**Tools:** `get_finding`, `list_findings`, `nearest_neighbors`, `search_detections`

**Steps:**
1. Retrieve full finding details and related context
2. Use `nearest_neighbors` to find similar findings via embedding similarity
3. Search detection rules for matching patterns
4. Reconstruct timeline of events
5. Identify all affected entities: IPs, hostnames, user accounts, file hashes
6. Determine attack vector and root cause
7. Document chain of evidence

**Output:** Root cause, attack vector, affected entities (IPs/hosts/users), evidence chain, related findings, timeline

### Phase 3: Contain & Respond (Responder Agent)

**Purpose:** NIST IR containment -- isolate hosts, block IPs, revoke credentials, plan remediation.

**Tools:** `create_approval_action`, `get_finding`, `update_case`

**Steps:**
1. Review investigation results and affected entities
2. Assess blast radius -- what systems/users/data are at risk
3. Plan containment actions with confidence scores:
   - 0.95-1.0: Critical threat (ransomware, active C2) -- auto-approve
   - 0.85-0.94: High confidence (confirmed malware) -- quick review
   - 0.70-0.84: Moderate (suspicious activity) -- human approval required
   - Below 0.70: Needs more investigation
4. Submit containment actions via `create_approval_action`
5. Define eradication steps (remove malware, patch vulnerabilities, revoke credentials)
6. Plan recovery and monitoring

**Output:** Containment actions with confidence scores, approval requests, remediation checklist, blast radius assessment

### Phase 4: Document & Report (Reporter Agent)

**Purpose:** Generate audience-tailored incident report with executive summary, technical details, and lessons learned.

**Tools:** `get_case`, `list_findings`, `create_attack_layer`

**Steps:**
1. Gather all data from prior phases (case, findings, actions taken)
2. Generate MITRE ATT&CK Navigator layer for the incident
3. Structure report with audience-tailored sections:
   - **Executive Summary:** Business impact in plain language
   - **Technical Details:** Evidence chain for the security team
   - **Timeline:** Chronological event reconstruction
   - **Actions Taken:** Containment and response measures
   - **Recommendations:** Preventive measures and next steps
   - **Lessons Learned:** What to improve

**Output:** Complete incident report, MITRE ATT&CK layer, event timeline, recommendations

## Example Invocation

```
User: "Run incident response on finding f-20260215-a1b2c3d4"
```

## Expected Output

```json
{
  "workflow": "incident-response",
  "phases_completed": ["triage", "investigation", "response", "report"],
  "severity": "critical",
  "category": "malware",
  "affected_entities": {
    "hosts": ["HOST-42"],
    "ips": ["10.0.1.15", "185.220.101.1"],
    "users": ["jsmith"]
  },
  "containment_actions": [
    {"action": "isolate_host", "target": "HOST-42", "confidence": 0.95, "status": "auto-approved"}
  ],
  "mitre_techniques": ["T1059.001", "T1071.001", "T1486"],
  "report_sections": ["executive_summary", "technical_details", "timeline", "actions", "recommendations"]
}
```
