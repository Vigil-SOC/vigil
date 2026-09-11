---
name: full-investigation
description: "Comprehensive multi-agent investigation with MITRE ATT&CK mapping, cross-signal correlation, response planning, and detailed reporting."
use_case: "Deep-dive investigation into suspicious findings or clusters, going beyond triage into full MITRE mapping, cross-signal correlation, and comprehensive response."
trigger_examples:
  - "Fully investigate this finding and all related activity"
  - "Do a complete investigation of case CASE-20260215-xyz"
  - "Deep dive into these suspicious lateral movement findings"
  - "Run full investigation workflow on this cluster of alerts"
objectives:
  - "Collect every entity and artifact the finding touches"
  - "Map the activity to ATT&CK and place it on the kill chain"
  - "Correlate related signals into attack chains and campaigns"
  - "Plan containment across the full correlated scope and report it"
phases:
  - id: evidence-gathering
    agent: investigator
    name: "Evidence Gathering"
    tools: [get_finding, list_findings, search_detections, recall_entity]
    instructions: |
      Retrieve finding details, collect surrounding context, reconstruct the
      timeline and identify all entities.

      1. Retrieve the target finding(s) via `get_finding`
      2. Search detection rules for matching patterns and coverage
      3. Build an entity inventory: all IPs, hostnames, user accounts and file
         hashes encountered
      4. Reconstruct an initial timeline from available timestamps
      5. Collect all evidence artifacts for the later steps

      Hand on the entity inventory, the evidence collection, the initial
      timeline and the related findings.

  - id: attack-mapping
    agent: mitre_analyst
    name: "ATT&CK Mapping"
    tools: [get_technique_rollup, create_attack_layer, get_finding, recall_entity]
    instructions: |
      Map all findings to MITRE ATT&CK techniques, assess kill chain progression
      and identify detection gaps.

      1. Extract all MITRE technique IDs from findings and related alerts
      2. Map techniques to ATT&CK tactics (Recon -> Initial Access -> Execution
         -> Persistence -> Privilege Escalation -> ... -> Impact)
      3. Assess kill chain progression -- how far has the attacker advanced?
      4. Identify gaps in the kill chain (missing visibility)
      5. Evaluate adversary sophistication based on TTPs
      6. Generate an ATT&CK Navigator layer visualisation
      7. Recommend detection rules for coverage gaps

      Hand on the technique IDs with confidence, the kill chain stage, the
      Navigator layer, the coverage gaps and the sophistication profile. A gap
      in visibility is a finding in its own right: say where you could not look.

  - id: correlation
    agent: correlator
    name: "Cross-Signal Correlation"
    tools: [list_findings, create_case, get_technique_rollup, recall_entity]
    instructions: |
      Link related alerts across time, entity and technique dimensions. Identify
      attack chains and campaigns.

      1. Gather all findings from the earlier steps
      2. Identify correlation signals:
         - Time proximity (within minutes/hours): +0.2
         - Entity overlap (shared IPs/hosts/users): +0.3
         - MITRE technique chain (sequential tactics): +0.4
      3. Score correlation strength for each alert pair
      4. Build the attack chain narrative: what happened in what order
      5. Identify campaign-level patterns (same actor across multiple incidents)
      6. Group correlated alerts into cases via `create_case`

      Hand on the correlated groups, the attack chain narrative, any campaign
      identification, the correlation scores and the new case groupings.

  - id: response-planning
    agent: responder
    name: "Response Planning"
    tools: [create_approval_action, update_case, get_finding, recall_entity]
    approval_required: true
    instructions: |
      Based on the full correlated scope, plan containment across all affected
      entities.

      1. Review the full attack scope from the correlation results
      2. Prioritise containment by blast radius (most impacted systems first)
      3. Plan containment actions with confidence scoring:
         - 0.95-1.0: auto-approve (active C2, ransomware, confirmed compromise)
         - 0.85-0.94: quick review (high-confidence threat indicators)
         - 0.70-0.84: human approval required (suspicious but unconfirmed)
      4. Submit actions via `create_approval_action`
      5. Define the eradication and recovery timeline
      6. Plan post-recovery monitoring

      Hand on the prioritised containment plan, the approval requests with their
      confidence, the remediation timeline and the recovery steps.

  - id: report
    agent: reporter
    name: "Comprehensive Report"
    tools: [get_case, list_findings, create_attack_layer, recall_entity]
    instructions: |
      Assemble the full investigation report from every artifact the earlier
      steps produced.

      1. Compile all step outputs into a structured narrative
      2. Generate the final MITRE ATT&CK Navigator layer
      3. Structure the report:
         - **Executive Summary:** business impact, risk assessment
         - **Investigation Timeline:** chronological reconstruction
         - **MITRE ATT&CK Analysis:** techniques, tactics, kill chain
         - **Correlation Results:** attack chains, campaigns
         - **Affected Assets:** complete entity inventory with impact
         - **Response Actions:** containment, eradication, recovery
         - **Detection Gaps:** what we missed and how to fix it
         - **Recommendations:** strategic and tactical improvements

      Carry the detection gaps through rather than quietly dropping them: a
      report that reads the same whether or not anyone could look is worse than
      one that admits what was not covered.
---

# Full Investigation Workflow

The most thorough investigation workflow available. Sequences five specialized agents to gather evidence, map to MITRE ATT&CK, correlate across signals, plan response, and produce a comprehensive report. Use when a finding warrants deep analysis beyond triage.

## When to Use

- A finding or cluster of findings warrants deep analysis
- You need complete MITRE ATT&CK technique mapping
- Multiple related alerts need cross-correlation
- A case requires comprehensive investigation before response
- Post-triage escalation for high/critical findings

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
