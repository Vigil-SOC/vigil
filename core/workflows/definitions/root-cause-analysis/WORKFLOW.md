---
name: root-cause-analysis
description: "Given a confirmed compromise, work backward along the kill chain to establish how it began — the initial access vector, patient zero, delivery and first execution — then how it escalated and spread."
use_case: "Root-cause analysis of a confirmed intrusion -- start from a proven finding (host, malware, C2, timeframe) and reconstruct how the attacker got in and moved, so the response closes the right door."
trigger_examples:
  - "A host was confirmed beaconing to C2 -- find how it was first compromised"
  - "Trace this proven PowerShell implant back to its initial access vector"
  - "Root cause: which email or download delivered the payload to patient zero"
  - "Reconstruct the kill chain backward from this confirmed exfiltration"
  - "How did the attacker reach this database -- work back from the confirmed access"
# The hypothesis loop run in reverse, not a phase chain: the lead decides what to
# test next from what the evidence has done to each origin belief, and a phase order
# cannot express a backward walk whose next question is the last answer's subject.
run_kind: root_cause

# An RCA is teed up automatically when a threat hunt hands off to IR, so it parks
# for the operator to go ahead before spending a model call -- the same
# hypothesis_approval gate a threat hunt raises at its own start. ask suspends the
# run until a human approves; the other checkpoints keep their auto defaults.
checkpoints:
  hypothesis_approval: ask

# Deliberately empty. What a run traces back is a claim about one confirmed
# compromise on one estate, supplied by the caller (the triggering finding: host,
# malware, C2, timeframe). A run with no hypothesis from the caller is refused --
# there is no default origin to assume. The benign account (this was an ordinary
# download / admin action / legitimate service) needs no stating: the controller
# seeds it as the null on every run, because it is the claim to beat.
hypotheses: []

# The vocabulary a worker's technique citation is gated against. It leans to the
# early kill chain -- delivery, initial access, execution, escalation, persistence --
# because that is what a backward walk legitimately finds, while still spanning the
# lateral movement and discovery a root cause reaches on its way out. A citation
# outside this list is refused at schema level; add to it rather than working around.
attack_techniques:
  # How it arrived
  - T1566.001   # Spearphishing attachment
  - T1566.002   # Spearphishing link
  - T1566.003   # Spearphishing via service
  - T1189       # Drive-by compromise
  - T1190       # Exploit public-facing application
  - T1195       # Supply chain compromise
  - T1133       # External remote services
  - T1078       # Valid accounts
  # The click, and the code it ran
  - T1204.001   # Malicious link
  - T1204.002   # Malicious file
  - T1203       # Exploitation for client execution
  - T1059.001   # PowerShell
  - T1059.003   # Windows command shell
  - T1059.005   # Visual Basic (macros)
  - T1105       # Ingress tool transfer
  # Staying, and rising
  - T1547.001   # Run keys / startup folder
  - T1053.005   # Scheduled task
  - T1543.003   # Windows service
  - T1548.002   # Bypass UAC
  - T1055       # Process injection
  - T1027       # Obfuscated / encoded payloads
  # Looking around and moving, on the way out
  - T1057       # Process discovery
  - T1046       # Network service discovery
  - T1021.001   # RDP
  - T1021.002   # SMB / admin shares
  - T1570       # Lateral tool transfer
  - T1071.001   # Web C2 (the confirmed behaviour a run starts from)
# The telemetry vocabulary, and a contract rather than a hint: a worker's
# source_system is constrained to this list at spec build, and corroboration is
# counted over distinct entries. A backward walk lives in endpoint and delivery
# telemetry, so those lead; a domain missing here is one no worker can name.
data_domains:
  - endpoint
  - process_lineage
  - win_events
  - email
  - http
  - proxy
  - dns
  - net_flow
  - auth
  - cloud

objectives:
  - "State an origin hypothesis for the confirmed compromise and the scope that would test it"
  - "Reconstruct the kill chain backward: C2/behaviour -> execution -> delivery -> initial access"
  - "Enrich the delivery-path observables (download source, sender, payload hash) where the evidence supports it"
  - "Report the initial access vector as proven, refuted or inconclusive, and what a responder should close first"
# The roster, not an order: the lead dispatches whichever of these the question in
# front of it needs. Their prompts and tool grants live in arch/rootcause.yaml, so
# what stands here is who can be asked and what each is for.
phases:
  - id: threat_hunter
    agent: threat_hunter
    name: "Endpoint reconstruction"
    tools: [findings_search, telemetry_search]
    instructions: |
      Walk the host backward: process lineage (which parent spawned the confirmed
      malicious child), file-create events (what wrote the payload and where), and
      the earliest activity before the compromise showed. "Nothing matched" is a
      finding about visibility, not a failure: say which sources you queried.

  - id: network_analyst
    agent: network_analyst
    name: "Delivery and egress"
    tools: [telemetry_search, findings_search]
    instructions: |
      Find how the payload arrived over the wire -- the download, mail pull or web
      request around the time the file first appeared -- and tie its external
      address and timing to the file-create on the host. Quantify.

  - id: threat_intel
    agent: threat_intel
    name: "Delivery-path enrichment"
    tools: [indicator_lookup]
    instructions: |
      Reputation and attribution for the delivery-path observables: the download
      domain, the sender infrastructure, the payload hash. A miss is not
      exoneration: say "not in the feed" and never report an unknown as benign.
---

# Root Cause Analysis Workflow

Backward, hypothesis-driven reconstruction of a confirmed intrusion. This text is the run's narrative — the Lead reads it as standing context for every decision it makes.

A root-cause run does not re-prove that something bad happened; the triggering finding already did that, and names the host, the malicious behaviour, the C2 or the timeframe. It starts from that confirmed point and works backward along the kill chain: from the C2 process, what spawned it; from that process, what wrote or launched it; from that file, how it reached the host — a download, an email attachment, a mounted share, a web exploit. Each answer is the next question's subject, and the earliest step the telemetry can attest that the estate did not do to itself is the root cause.

It does not walk a sequence of steps. It puts one or more origin hypotheses on the board and each iteration the Lead reads a digest of what has been gathered and chooses what to do next: dispatch a worker against an open question, expand a piece of evidence, pivot onto the process or file one layer earlier, deepen a line that is paying off, abandon one that is not, validate an origin it believes is settled, stop and ask an operator, or conclude. What the evidence did to each belief drives the next move, which is why there is no phase order.

Every origin hypothesis ends as proven, refuted or inconclusive. Inconclusive is a legitimate ending and must be reported as itself: distinguish "the trail reached the delivery and it was a phishing attachment" from "the endpoint telemetry did not retain far enough back to say" — they read identically in a report that does not separate them, and only one of them names a root cause.

An illustrative shape (not a template to match): a confirmed PowerShell C2 beacon traces back through the process that spawned it to a file written into a browser's download folder — a malicious `.lnk` a user was lured into opening — which is the initial access. The real chain is whatever this estate's telemetry attests; do not assume this one.

## When to Use

- After a hunt (or a detection) has confirmed a compromise and you need to know how it began
- Establishing patient zero and the delivery vector so the response closes the right door
- Reconstructing the kill chain backward from a proven C2, implant, or exfiltration
- Determining which users, hosts or channels received the same lure

## Example Invocation

```
User: "FYODOR-L is confirmed beaconing to 45.77.53.176 via encoded PowerShell. Find how it was first compromised."
```

## Expected Output

The run's own ledger, and a report rendered from it. The console reads the standing
of each origin hypothesis while the run is in flight:

```json
{
  "status": "terminal",
  "iteration": 6,
  "evidence_count": 28,
  "hypotheses": [
    {
      "statement": "The host was initially compromised via a malicious file delivered to the user and executed from a browser download",
      "status": "proven",
      "attack_technique": "T1204.002",
      "resolution_reason": "file-create of the payload in the Edge download folder 2s after an external GET, 3 minutes before the first encoded-PowerShell process"
    },
    {
      "statement": "The same lure reached other users in the estate",
      "status": "inconclusive",
      "attack_technique": "T1566.001",
      "resolution_reason": "mail telemetry not carried by this deployment; could not look"
    }
  ]
}
```
