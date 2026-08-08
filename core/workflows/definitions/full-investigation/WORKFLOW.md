---
name: full-investigation
description: "Comprehensive multi-agent investigation with MITRE ATT&CK mapping, cross-signal correlation, response planning, and detailed reporting."
agents:
  - investigator
  - mitre_analyst
  - correlator
  - responder
  - reporter
tools-used:
  - get_finding
  - list_findings
  - nearest_neighbors
  - search_detections
  - get_technique_rollup
  - create_attack_layer
  - create_case
  - create_approval_action
  - update_case
  - get_case
use-case: "Deep-dive investigation into suspicious findings or clusters, going beyond triage into full MITRE mapping, cross-signal correlation, and comprehensive response."
trigger-examples:
  - "Fully investigate this finding and all related activity"
  - "Do a complete investigation of case CASE-20260215-xyz"
  - "Deep dive into these suspicious lateral movement findings"
  - "Run full investigation workflow on this cluster of alerts"
---

# Full Investigation Workflow

The most thorough investigation workflow available. Sequences five specialized agents to gather evidence, map to MITRE ATT&CK, correlate across signals, plan response, and produce a comprehensive report. Use when a finding warrants deep analysis beyond triage.

## When to Use

- A finding or cluster of findings warrants deep analysis
- You need complete MITRE ATT&CK technique mapping
- Multiple related alerts need cross-correlation
- A case requires comprehensive investigation before response
- Post-triage escalation for high/critical findings

## Agent Sequence

### Phase 1: Evidence Gathering (Investigator Agent)

**Purpose:** Retrieve finding details, collect surrounding context, reconstruct timeline, identify all entities.

**Tools:** `get_finding`, `list_findings`, `nearest_neighbors`, `search_detections`

**Steps:**
1. Retrieve the target finding(s) via `get_finding`
2. Use `nearest_neighbors` to discover related findings via embedding similarity
3. Search detection rules for matching patterns and coverage
4. Build entity inventory: all IPs, hostnames, user accounts, file hashes encountered
5. Reconstruct initial timeline from available timestamps
6. Collect all evidence artifacts for downstream phases

**Output:** Entity inventory (IPs, hosts, users, hashes), evidence collection, initial timeline, related findings via embeddings

### Phase 2: ATT&CK Mapping (MITRE Analyst Agent)

**Purpose:** Map all findings to MITRE ATT&CK techniques, assess kill chain progression, identify detection gaps.

**Tools:** `get_technique_rollup`, `create_attack_layer`, `get_finding`, detection coverage tools

**Steps:**
1. Extract all MITRE technique IDs from findings and related alerts
2. Map techniques to ATT&CK framework tactics (Recon -> Initial Access -> Execution -> Persistence -> Privilege Escalation -> ... -> Impact)
3. Assess kill chain progression -- how far has the attacker advanced?
4. Identify gaps in the kill chain (missing visibility)
5. Evaluate adversary sophistication based on TTPs
6. Generate ATT&CK Navigator layer visualization
7. Recommend detection rules for coverage gaps

**Output:** Technique IDs with confidence, kill chain stage assessment, ATT&CK Navigator layer, detection coverage gaps, adversary sophistication profile

### Phase 3: Cross-Signal Correlation (Correlator Agent)

**Purpose:** Link related alerts across time, entity, and technique dimensions. Identify attack chains and campaigns.

**Tools:** `list_findings`, `create_case`, `get_technique_rollup`, `nearest_neighbors`

**Steps:**
1. Gather all findings from Phases 1-2
2. Identify correlation signals:
   - Time proximity (within minutes/hours): +0.2
   - Entity overlap (shared IPs/hosts/users): +0.3
   - MITRE technique chain (sequential tactics): +0.4
3. Score correlation strength for each alert pair
4. Build attack chain narrative: what happened in what order
5. Identify campaign-level patterns (same actor across multiple incidents)
6. Group correlated alerts into cases via `create_case`

**Output:** Correlated alert groups, attack chain narrative, campaign identification, correlation scores, new case groupings

### Phase 4: Response Planning (Responder Agent)

**Purpose:** Based on the full correlated scope, plan containment across all affected entities.

**Tools:** `create_approval_action`, `update_case`, `get_finding`

**Steps:**
1. Review the full attack scope from correlation results
2. Prioritize containment by blast radius (most impacted systems first)
3. Plan containment actions with confidence scoring:
   - 0.95-1.0: Auto-approve (active C2, ransomware, confirmed compromise)
   - 0.85-0.94: Quick review (high-confidence threat indicators)
   - 0.70-0.84: Human approval required (suspicious but unconfirmed)
4. Submit actions via `create_approval_action`
5. Define eradication and recovery timeline
6. Plan post-recovery monitoring

**Output:** Prioritized containment plan, approval requests with confidence, remediation timeline, recovery steps

### Phase 5: Comprehensive Report (Reporter Agent)

**Purpose:** Full investigation report assembling all artifacts from all prior phases.

**Tools:** `get_case`, `list_findings`, `create_attack_layer`

**Steps:**
1. Compile all phase outputs into a structured narrative
2. Generate final MITRE ATT&CK Navigator layer
3. Structure the comprehensive report:
   - **Executive Summary:** Business impact, risk assessment
   - **Investigation Timeline:** Chronological reconstruction
   - **MITRE ATT&CK Analysis:** Techniques, tactics, kill chain
   - **Correlation Results:** Attack chains, campaigns
   - **Affected Assets:** Complete entity inventory with impact
   - **Response Actions:** Containment, eradication, recovery
   - **Detection Gaps:** What we missed and how to fix it
   - **Recommendations:** Strategic and tactical improvements

**Output:** Full investigation report, ATT&CK visualization, timeline, correlated findings summary, recommendations

## Example Invocation

```
User: "Run full investigation on finding f-20260215-deadbeef"
```

## Expected Output

```json
{
  "workflow": "full-investigation",
  "phases_completed": ["evidence-gathering", "attack-mapping", "correlation", "response-planning", "report"],
  "entities_discovered": {
    "hosts": ["HOST-42", "HOST-17", "DC-01"],
    "ips": ["10.0.1.15", "10.0.1.22", "185.220.101.1"],
    "users": ["jsmith", "admin-svc"],
    "hashes": ["a1b2c3..."]
  },
  "mitre_techniques": ["T1078", "T1059.001", "T1071.001", "T1021.002", "T1486"],
  "kill_chain_stage": "lateral_movement",
  "correlated_findings": 8,
  "correlation_score": 0.87,
  "containment_actions": 3,
  "report_sections": ["executive_summary", "timeline", "mitre_analysis", "correlation", "assets", "response", "gaps", "recommendations"]
}
```
