---
name: root-cause-analysis
version: 1
description: "Given a confirmed compromise, trace backward event by event to where it began, and record each step only when the value that ties it to the one before checks out."
use_case: "Root-cause analysis of a confirmed intrusion -- start from a proven finding and reconstruct how the attacker got in, so the response closes the right door."
trigger_examples:
  - "A host was confirmed beaconing to C2 -- find how it was first compromised"
  - "Trace this proven PowerShell implant back to its initial access vector"
  - "Root cause: which email or download delivered the payload to patient zero"
  - "Reconstruct the chain backward from this confirmed exfiltration"
  - "How did the attacker reach this database -- work back from the confirmed access"
run_kind: root_cause
objectives:
  - "Record each causal step and the value that ties it to the step before"
  - "Prove a link only from telemetry this run was shown, never from a paraphrase"
  - "Stop at an origin only when nothing earlier carries that link"
  - "Report what is still unproven when the cost or time ceiling stops the trace"
---

# Root Cause Analysis

A confirmed finding is the start, not a hypothesis to re-test. Walk backward one event at a time: what produced the artifact this step used, and which earlier event produced that, until nothing earlier exists.

The tie between two events is a value both of them carry. An IP, a timestamp, or the actor stamped on either event is not that tie. The trace names someone only when a proven step carries that name.
