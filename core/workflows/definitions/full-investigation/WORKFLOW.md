---
name: full-investigation
description: "Comprehensive investigation with MITRE ATT&CK mapping, cross-signal correlation, response planning, and detailed reporting."
use_case: "Deep-dive investigation into suspicious findings or clusters, going beyond triage into full MITRE mapping, cross-signal correlation, and comprehensive response."
trigger_examples:
  - "Fully investigate this finding and all related activity"
  - "Do a complete investigation of case CASE-20260215-xyz"
  - "Deep dive into these suspicious lateral movement findings"
  - "Run full investigation workflow on this cluster of alerts"
run_kind: investigate
objectives:
  - "Collect every entity and artifact the finding touches"
  - "Map the activity to ATT&CK and place it on the kill chain"
  - "Correlate related signals into attack chains and campaigns"
  - "Plan containment across the full correlated scope and report it"
---

# Full Investigation Workflow

Investigate a finding or a cluster past triage. Collect every entity and artifact the finding touches. Map the activity to ATT&CK and place it on the kill chain. Correlate related signals into attack chains and campaigns. Plan containment across the full correlated scope and report it.
