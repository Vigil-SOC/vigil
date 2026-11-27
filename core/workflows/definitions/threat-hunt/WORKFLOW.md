---
name: threat-hunt
version: 3
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
# No technique list. The agent may cite any ATT&CK technique or sub-technique; each
# cited id is checked against the shipped catalogue (core/threat_intel/
# attack_techniques.json) when evidence is recorded, and one that is not active is
# not counted.
# The telemetry vocabulary, and a contract rather than a hint: the agent's
# source_system is constrained to this list at spec build, and corroboration is
# counted over distinct entries. A domain missing here is one the agent cannot name,
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
  # An AI agent's own activity (ai-agent-activity skill): its trace spans, the
  # LLM/MCP gateway in front of it, and the sandbox it executes in.
  - agent_spans
  - llm_gateway
  - agent_sandbox

objectives:
  - "State a hypothesis and the scope that would test it"
  - "Characterise the network and artifact evidence bearing on it"
  - "Enrich every observable and attribute where the evidence supports it"
  - "Report the hypothesis as confirmed, refuted or inconclusive, with reasons"
---

# Threat Hunt Workflow

Proactive, hypothesis-driven threat hunting. This text is the hunt's narrative — the Hunt Lead reads it as standing context for every decision it makes.

A hunt does not walk a sequence of steps. It puts the hypotheses above on the board, and the Hunt Lead works them itself: it queries the estate, records what came back as evidence, links each record against every hypothesis on the board — the benign account included — and puts a belief up for validation, where an independent critic argues the strongest benign explanation before any verdict stands. Along the way it may expand a piece of evidence, pivot onto an entity, deepen a line that is paying off, abandon one that is not, stop and ask for an operator, or conclude. What the evidence did to each belief is what drives the next move, which is why there is no phase order to state.

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
