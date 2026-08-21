---
name: threat-hunt
description: "Proactive, hypothesis-driven threat hunting across all available data sources with network analysis, malware examination, and intelligence enrichment."
use_case: "Proactive threat hunting -- start with a hypothesis or IOC and systematically search for evidence across network, endpoint, and threat intel sources."
trigger_examples:
  - "Hunt for C2 beaconing activity across all network findings"
  - "Proactive hunt: look for lateral movement via RDP"
  - "Validate whether this DeepTempo C2 alert is real by checking all available threat intel for the public IP"
  - "Hunt for APT28 credential harvesting techniques"
  - "Search for signs of data exfiltration in the last 24 hours"
# The hypothesis loop, not a phase chain: the lead decides what to test next from
# what the evidence has done to each belief, and a phase order cannot express that.
run_kind: hunt

# Deliberately empty. What a hunt is out to test is a claim about one estate at one
# moment, and a shipped default is a claim about neither: the two that used to sit
# here described beaconing and credential reuse, so a hunt for exfiltration opened
# on somebody else's scenario and spent its first turns there. The second also had
# no referent until the first was confirmed, which is a conditional dressed as a
# peer belief -- and an unresolvable one counts against the verdict gate.
#
# So the caller states the hypothesis, and the run is refused without one. The
# benign account needs no stating: the controller seeds it as the base rate on
# every hunt, because it is the claim to beat rather than an objection to raise.
hypotheses: []
# The vocabulary a worker's technique citation is gated against, and nothing else.
# It labels no hypothesis: pairing this list against `hypotheses` by position made
# their order load-bearing and asserted a technique nobody had checked. What a
# belief is about is what its evidence cited.
#
# A citation outside this list is refused at schema level, so the list has to span
# what a hunt over the data_domains below could legitimately find, not just what
# one scenario expects. Add to it rather than working around it. Empty declares no
# vocabulary at all, which gates nothing.
attack_techniques:
  # Command and control, and the channels it hides in
  - T1071.001   # Web protocols
  - T1071.004   # DNS
  - T1568.002   # Domain generation algorithms
  - T1573       # Encrypted channel
  - T1090       # Proxy
  # Getting data out
  - T1041       # Exfiltration over C2 channel
  - T1048       # Exfiltration over alternative protocol
  - T1567       # Exfiltration over web service
  # Getting in
  - T1190       # Exploit public-facing application
  - T1566.001   # Spearphishing attachment
  - T1566.002   # Spearphishing link
  - T1133       # External remote services
  # Credentials and the accounts they open
  - T1078       # Valid accounts
  - T1110       # Brute force
  - T1003       # OS credential dumping
  - T1550.002   # Pass the hash
  - T1098       # Account manipulation
  - T1552.005   # Cloud instance metadata API
  # Moving through the estate
  - T1021.001   # RDP
  - T1021.002   # SMB / admin shares
  - T1570       # Lateral tool transfer
  # Running code, and staying
  - T1059.001   # PowerShell
  - T1059.003   # Windows command shell
  - T1053.005   # Scheduled task
  - T1543.003   # Windows service
  - T1547.001   # Run keys / startup folder
  - T1055       # Process injection
  # Looking around
  - T1046       # Network service discovery
  - T1018       # Remote system discovery
  - T1087       # Account discovery
  # Covering tracks, and what it was all for
  - T1562.001   # Disable or modify tools
  - T1070.004   # File deletion
  - T1530       # Data from cloud storage
  - T1486       # Data encrypted for impact
  - T1496       # Resource hijacking
# The telemetry vocabulary, and a contract rather than a hint: a worker's
# source_system is constrained to this list at spec build, and corroboration is
# counted over distinct entries. A domain missing here is one no worker can name,
# so this must describe the telemetry the deployment actually carries.
data_domains:
  - net_flow
  - dns
  - http
  - proxy
  - endpoint
  - process_lineage
  - win_events
  - auth
  - email
  - cloud

objectives:
  - "State a hypothesis and the scope that would test it"
  - "Characterise the network and artifact evidence bearing on it"
  - "Enrich every observable and attribute where the evidence supports it"
  - "Report the hypothesis as confirmed, refuted or inconclusive, with reasons"
# The roster, not an order: the lead dispatches whichever of these the question in
# front of it needs. Their prompts and tool grants live in arch/threathunt.yaml,
# so what stands here is who can be asked and what each is for.
phases:
  - id: threat_hunter
    agent: threat_hunter
    name: "Behavioural hunting"
    tools: [search_findings, nearest_neighbors, telemetry_search]
    instructions: |
      Broad behavioural hunting across the signal detection already scored and the
      telemetry behind it. "Nothing matched" is a finding about visibility, not a
      failure: say which sources you queried and which you could not.

  - id: network_analyst
    agent: network_analyst
    name: "Traffic shape"
    tools: [telemetry_search, search_findings]
    instructions: |
      Beaconing intervals, jitter, volume asymmetry, DNS and HTTP. Quantify: a
      regular interval with low variance is the signal, a busy host is not.

  - id: threat_intel
    agent: threat_intel
    name: "Observable enrichment"
    tools: [lookup_indicators]
    instructions: |
      Reputation and attribution for observables, against the indicator database
      and whatever intel integrations are connected. A miss is not exoneration:
      say "not in the feed" and never report an unknown observable as benign.
---

# Threat Hunt Workflow

Proactive, hypothesis-driven threat hunting. This text is the hunt's narrative — the Hunt Lead reads it as standing context for every decision it makes.

A hunt does not walk a sequence of steps. It puts the hypotheses above on the board, and each iteration the Hunt Lead reads a digest of what has been gathered so far and chooses what to do next: dispatch a worker against an open question, expand a piece of evidence it was shown, pivot onto an entity, deepen a line that is paying off, abandon one that is not, validate a hypothesis it believes is settled, stop and ask for an operator, or conclude. What the evidence did to each belief is what drives the next move, which is why there is no phase order to state.

Every hypothesis ends as proven, disproven or inconclusive. Inconclusive is a legitimate ending and must be reported as itself: distinguish "we looked and it was not there" from "we could not look" — they read identically in a report that does not separate them, and only one of them clears the hypothesis.

## When to Use

- Proactive hunting for threats that haven't triggered alerts
- Validating a flagged alert (e.g., DeepTempo C2 detection) against all available sources
- Hypothesis-driven hunting based on specific TTPs or threat actors
- Searching for indicators of compromise across the environment
- Periodic threat hunting exercises

## Example Invocation

```
User: "Validate whether this DeepTempo C2 alert is real by checking all threat intel for IP 185.220.101.1"
```

## Expected Output

The run's own ledger, and a hunt report rendered from it. The console reads the
standing of each hypothesis while the hunt is in flight:

```json
{
  "status": "terminal",
  "iteration": 7,
  "evidence_count": 34,
  "hypotheses": [
    {
      "statement": "A host is beaconing to attacker-controlled infrastructure on a regular interval",
      "status": "proven",
      "attack_technique": "T1071.001",
      "resolution_reason": "300s interval, variance under 4s, across 19 hours to 185.220.101.1"
    },
    {
      "statement": "Credentials taken from that host have been reused elsewhere in the estate",
      "status": "inconclusive",
      "attack_technique": "T1078",
      "resolution_reason": "authentication telemetry retained for 7 days; the beaconing predates it"
    }
  ]
}
```
