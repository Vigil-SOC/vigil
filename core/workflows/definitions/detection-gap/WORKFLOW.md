---
name: detection-gap
version: 1
description: "Map a case to ATT&CK, find what no rule caught, propose detections."
use_case: "Detection gap review -- after a case closes, or when a new threat report lands, say which techniques no loaded rule covers and draft a rule for each. Reads only: a draft stays in the report and is pushed nowhere."
trigger_examples:
  - "What did we miss in this case?"
  - "Review detection coverage for this report"
# One phase, so no step order: the agent decides what to read and when it is done.
run_kind: compose
objectives:
  - "Name every technique the case shows, by tactic"
  - "Say which techniques no loaded rule covers"
  - "Draft a detection for each uncovered technique, and push none of them"
  - "State what the data cannot answer"
phases:
  - id: review
    name: Detection gap review
    agent: mitre_analyst
    instructions: |
      Say what no rule caught for the case or report you were given.

      identify_gaps sets the techniques on stored alerts against the rules loaded;
      pass the case id when you have one. Read its answer and unknown fields first
      and repeat them: when it answers unknown, the data cannot say, and your report
      says that rather than a guess. An alert that names a tactic and no technique
      does not show which technique ran. If its record shows you one, name it and
      mark it as your reading, not as what the alert said.

      For each technique no rule covers, draft a Sigma rule and test it with
      check_detection_candidate. A draft stays in your report. You hold no tool that
      deploys or writes anything.

      Expected output: the techniques seen by tactic, the uncovered ones with a draft
      rule and its check result for each, and what could not be answered.
    tools:
      - identify_gaps
      - analyze_coverage
      - search_detections
      - check_detection_candidate
      - get_technique_rollup
      - get_case
      - case_records
      - get_finding
---

# Detection gap review

Map a case or a threat report to ATT&CK, find what no loaded rule caught, and propose detections. A person starts this workflow. It reads stored alerts and the detection rules loaded on this deployment, and it writes nothing: a draft rule stays in the run's report.

When the rule sources are not installed, or the alerts name tactics and no techniques, the run says the data cannot answer rather than naming a gap.
