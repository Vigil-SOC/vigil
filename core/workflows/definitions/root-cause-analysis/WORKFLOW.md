---
name: root-cause-analysis
description: "Given a confirmed compromise, trace it backward to how it began — each step tied to the one before by a value both events carry — and report the chain, what is still open, and what to close first."
use_case: "Root-cause analysis of a confirmed intrusion -- start from a proven finding (host, malware, C2, timeframe) and reconstruct how the attacker got in and moved, so the response closes the right door."
trigger_examples:
  - "A host was confirmed beaconing to C2 -- find how it was first compromised"
  - "Trace this proven PowerShell implant back to its initial access vector"
  - "Root cause: which email or download delivered the payload to patient zero"
  - "Reconstruct the kill chain backward from this confirmed exfiltration"
  - "How did the attacker reach this database -- work back from the confirmed access"
# Its own loop, not the hunt's: one investigator searching the logs and keeping a
# findings notebook whose causal links are checked in code. It needs Splunk: the
# checks are SPL, and a deployment searching logs with anything else is told so
# before the run spends anything.
run_kind: root_cause

# A trace is teed up automatically when a threat hunt hands off to IR, so it waits
# for an operator to permit the trace of that finding before spending a model call.
checkpoints:
  rca_permit: ask
---

# Root Cause Analysis

A root-cause run does not re-prove that something bad happened; the finding it was given already did that. It starts from that confirmed point and works backward: from the process that beaconed, what launched it; from that process, what it ran and where that came from; from that file, how it reached the host. Each answer is the next question's subject.

A step is proven only by what links it to the step before: a value the earlier event produced and the later one used — a process id, a file name or hash, a commit, an image, a job or request id, a key id. A shared IP, a time or an identity is not a link. A link that cannot be proven is unknown, not absent, and the report says so rather than calling it unrelated.

The earliest step is an origin only if nothing in the logs mentions its actor earlier; a busy account that merely appears first in the logs is not the start of an attack, and how it came to be used is its own open question.

The report gives the chain from the earliest proven step to the confirmed compromise, the artifact, event and time behind each step, what is still open and why, and what a responder should close first. It names someone as the attacker only through proven links.

## When to Use

- After a hunt (or a detection) has confirmed a compromise and you need to know how it began
- Establishing patient zero and the delivery vector so the response closes the right door
- Reconstructing the kill chain backward from a proven C2, implant, or exfiltration

## Example Invocation

```
User: "FYODOR-L is confirmed beaconing to 45.77.53.176 via encoded PowerShell. Find how it was first compromised."
```
