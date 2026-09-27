---
name: shadow-adjudication
description: "A shadow second opinion on a finding intake has already admitted: test the stated intent against the benign account, name the workflow that should have run, and execute nothing."
use_case: "Shadow adjudication -- given a finding intake admitted and the workflow it chose, decide independently whether the finding is what intake says it is and which catalogue workflow you would have run, so intake's choices can be scored later."
trigger_examples:
  - "Adjudicate: intake admitted this C2 finding and chose incident-response -- would you have?"
  - "Second opinion on an admitted lateral-movement finding routed to threat-hunt"
  - "Shadow-check the workflow intake picked for this credential-reuse finding"
  - "Is this admitted exfiltration finding real, and which workflow does it warrant?"
# The hypothesis loop, not a phase chain: the lead decides what to test next from
# what the evidence has done to each belief, and a phase order cannot express that.
# adjudicate is the hunt loop with a lead framed as an adjudicator: it proposes a
# workflow from the catalogue and starts none.
run_kind: adjudicate

# Deliberately empty, as threat-hunt is. The caller states the hypothesis -- the
# finding intake admitted and the intent it read into it -- and the run is refused
# without one. The benign account needs no stating: the controller seeds it as the
# base rate on every run, because it is the claim the finding has to beat.
hypotheses: []
# Copied from threat-hunt: the vocabulary a worker's technique citation is gated
# against, and nothing else. An adjudication may be handed a finding of any kind
# intake admits, so the list has to span what a hunt over the data_domains below
# could legitimately find. Add to it rather than working around it.
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
# Copied from threat-hunt: a worker's source_system is constrained to this list at
# spec build, and corroboration is counted over distinct entries.
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
  - "Take the admitted finding and intake's chosen workflow as the hypothesis under test"
  - "Test the stated intent against the seeded benign account with the same verbs"
  - "Name the catalogue workflow this finding warrants -- or none"
  - "State in one sentence whether intake's choice was right, and why; start nothing"
# The roster, not an order: the lead dispatches whichever of these the question in
# front of it needs. Their prompts and tool grants live in arch/adjudicate.yaml,
# so what stands here is who can be asked and what each is for.
phases:
  - id: threat_hunter
    agent: threat_hunter
    name: "Behavioural check"
    tools: [search_findings, telemetry_search]
    instructions: |
      Characterise the admitted finding against the signal detection already scored
      and the telemetry behind it. "Nothing matched" is a finding about visibility,
      not a failure: say which sources you queried and which you could not.

  - id: network_analyst
    agent: network_analyst
    name: "Traffic shape"
    tools: [telemetry_search, search_findings]
    instructions: |
      Beaconing intervals, jitter, volume asymmetry, DNS and HTTP around the
      finding's entities. Quantify: a regular interval with low variance is the
      signal, a busy host is not.

  - id: threat_intel
    agent: threat_intel
    name: "Observable enrichment"
    tools: [lookup_indicators]
    instructions: |
      Reputation and attribution for the finding's observables, against the
      indicator database and whatever intel integrations are connected. A miss is
      not exoneration: say "not in the feed" and never report an unknown observable
      as benign.
---

# Shadow Adjudication Workflow

A second, independent opinion on a finding intake has already admitted. This text is the run's narrative — the Lead reads it as standing context for every decision it makes.

The finding and the workflow intake chose arrive in the run request and the stated hypothesis. The Lead does not take the admission as given: it puts intake's stated intent on the board beside the seeded benign account and tests both with the same verbs a hunt uses — dispatch a worker against an open question, expand a piece of evidence, pivot onto one of the finding's entities, deepen a line that is paying off, abandon one that is not, validate a hypothesis it believes is settled, stop and ask an operator, or conclude. What the evidence did to each belief drives the next move, which is why there is no phase order.

The run executes nothing. There is no handoff to incident response and no workflow is started. Its CONCLUDE names the workflow the Lead would have run from the catalogue — `incident-response`, `forensic-analysis`, `full-investigation`, `threat-hunt`, `root-cause-analysis`, `cloud-incident` — or `none`, and says in one sentence whether it agrees with intake and why. That proposal is journaled with the decision so intake's choices can be scored against it later.

Every hypothesis ends as proven, disproven or inconclusive. Inconclusive is a legitimate ending and must be reported as itself: distinguish "we looked and it was not there" from "we could not look".

## When to Use

- Shadow-scoring intake's admissions and workflow choices before letting it act on them
- A second opinion on a single admitted finding whose routing is in doubt
- Building a record of where intake and an independent adjudicator disagree

## Example Invocation

```
User: "Intake admitted finding f-4821 (host 10.128.0.5 beaconing to 198.51.100.7 every 300s) and chose incident-response. Adjudicate."
```

## Expected Output

The run's own ledger, and a report rendered from it. The concluding decision carries the proposal:

```json
{
  "action": "CONCLUDE",
  "proposed_workflow": "incident-response",
  "rationale": "The 300s interval with sub-4s jitter to a single external host across 19 hours survived the benign account; this is a live C2 channel and warrants incident-response. I agree with intake: containment is the first move, and a hunt would only confirm what the telemetry already attests."
}
```
