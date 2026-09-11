---
name: incident-response
description: "Respond to active security incidents with rapid triage, deep investigation, containment, and documentation. Follows NIST IR framework."
use_case: "Active incident response -- an alert fires and the SOC needs to triage, investigate, contain, and document."
trigger_examples:
  - "Run incident response on finding f-20260215-abc123"
  - "We have an active incident -- ransomware detected on HOST-42"
  - "Respond to this critical alert"
  - "IR workflow for this phishing finding"
objectives:
  - "Classify the alert and decide whether it warrants response"
  - "Establish root cause, attack vector and blast radius"
  - "Contain the threat and plan eradication and recovery"
  - "Produce an audit-ready record of what happened and what was done"
phases:
  - id: triage
    agent: triage
    name: "Triage & Classify"
    tools: [get_finding, list_findings, recall_entity]
    instructions: |
      Rapidly assess the alert: severity scoring, false-positive check,
      categorisation.

      1. Fetch the finding via `get_finding` to retrieve full details
      2. Assess severity based on anomaly score, data source, and MITRE techniques
      3. Categorize the alert: malware, intrusion, policy violation,
         reconnaissance, exfiltration, or false positive
      4. Assign priority: Critical (immediate action), High (within 1hr),
         Medium (queue), Low (monitor), False Positive (dismiss)
      5. Make an escalation decision with reasoning

      Hand on the severity score, alert category, priority level and escalation
      decision. If you classify this as a false positive, say so plainly in the
      handoff: the later steps are written expecting a real incident, and a
      dismissal they cannot see reads exactly like a confirmed one.

  - id: investigation
    agent: investigator
    name: "Deep Investigation"
    tools: [get_finding, list_findings, search_detections, recall_entity]
    instructions: |
      Root cause analysis, evidence collection, timeline reconstruction and
      cross-source correlation.

      1. Retrieve full finding details and related context
      2. Search detection rules for matching patterns
      3. Reconstruct the timeline of events
      4. Identify all affected entities: IPs, hostnames, user accounts, file hashes
      5. Determine the attack vector and root cause
      6. Document the chain of evidence

      Hand on the root cause, attack vector, affected entities, evidence chain,
      related findings and timeline.

  - id: response
    agent: responder
    name: "Contain & Respond"
    tools: [create_approval_action, get_finding, update_case, recall_entity]
    approval_required: true
    instructions: |
      NIST IR containment: isolate hosts, block IPs, revoke credentials, plan
      remediation.

      1. Review the investigation results and affected entities
      2. Assess the blast radius -- what systems, users and data are at risk
      3. Plan containment actions with confidence scores:
         - 0.95-1.0: Critical threat (ransomware, active C2) -- auto-approve
         - 0.85-0.94: High confidence (confirmed malware) -- quick review
         - 0.70-0.84: Moderate (suspicious activity) -- human approval required
         - Below 0.70: needs more investigation
      4. Submit containment actions via `create_approval_action`
      5. Define eradication steps (remove malware, patch vulnerabilities,
         revoke credentials)
      6. Plan recovery and monitoring

      Hand on the containment actions with their confidence scores, the approval
      requests raised, the remediation checklist and the blast-radius assessment.

  - id: report
    agent: reporter
    name: "Document & Report"
    tools: [get_case, list_findings, create_attack_layer, recall_entity]
    instructions: |
      Generate an audience-tailored incident report with executive summary,
      technical detail and lessons learned.

      1. Gather all data from the prior steps (case, findings, actions taken)
      2. Generate a MITRE ATT&CK Navigator layer for the incident
      3. Structure the report with audience-tailored sections:
         - **Executive Summary:** business impact in plain language
         - **Technical Details:** evidence chain for the security team
         - **Timeline:** chronological event reconstruction
         - **Actions Taken:** containment and response measures
         - **Recommendations:** preventive measures and next steps
         - **Lessons Learned:** what to improve

      Where an earlier step reported a gap rather than an answer, name it. A
      report that reads the same whether or not anyone could look is worse than
      one that admits what was not covered.
---

# Incident Response Workflow

Multi-agent incident response workflow following the NIST Incident Response framework. Sequences four specialized agents to rapidly triage, deeply investigate, contain threats, and produce audit-ready documentation.

## When to Use

- A security alert has fired and needs immediate response
- A finding has been flagged as critical or high severity
- An active threat (malware, C2, data exfiltration) has been detected
- You need end-to-end incident handling from triage to report

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
