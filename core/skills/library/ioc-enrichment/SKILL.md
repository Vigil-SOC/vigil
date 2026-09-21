---
name: ioc-enrichment
description: Enrich the indicators of compromise (IPs, domains, hashes, URLs) in a finding. Read this before looking up any IOC so the enrichment checks memory first, uses every intel source you hold in parallel, and states provenance and confidence per source.
metadata:
  vigil-origin: threat_intel profile methodology (epic #882, #930)
---

# IOC enrichment

Use this procedure whenever a finding, alert or question carries indicators
that need external context. It applies to any agent: triage, investigation,
reporting or threat intelligence.

## Procedure

1. **Extract indicators.** Read the finding (`get_finding` if you only hold an
   id) and list every IP, domain, URL, hash, email and file path from
   `iocs`, `raw_data` and `description`. Separate internal addresses
   (RFC 1918, loopback, your own ranges) from external ones: internal hosts
   are pivots, not lookups.

2. **Recall before you look up.** Call `recall_entity` on each external
   indicator before spending an external lookup. Memory is read-only; it tells
   you what Vigil has seen about the entity before and orients the search. It
   never decides the outcome on its own.

3. **Enrich in parallel with whatever you hold.** Issue independent lookups in
   the same turn rather than one at a time. Use the tools present in your
   grant, not a fixed list:
   - If you hold `cf_lookup_ip_threat` or `cf_lookup_domain_threat`, use them
     for IPs and domains respectively.
   - If you hold `lookup_indicators`, query the local threat feed table for
     the indicator type and values.
   - If an MCP server exposes intel tools (reputation, passive DNS, WHOIS,
     sandbox, geolocation), use those too.
   - If none of these is callable in this turn, say so once and complete the
     report from what the finding and memory already contain, marking each
     absent source `not queried`. Never end a turn on an announced lookup,
     and never invent a lookup result.

4. **Identify actor and infrastructure context.** From the returned data, note
   TTPs, infrastructure overlap, campaign or malware family associations and
   first/last-seen dates. Attribution is a hypothesis: give it a confidence
   and the evidence behind it.

5. **State confidence and provenance per source.** For every enriched
   indicator, report each source by name, what that source said, and the
   confidence it carried (or your own, labelled as such). Where sources
   disagree, say which you weight more and why. Never merge sources into one
   unattributed verdict.

6. **Return indicators to hunt.** End with the actionable list: the
   indicators (and any related ones surfaced by enrichment) worth hunting
   across the environment, each with its type, source and confidence, plus the
   recommended next step (hunt, block, monitor, or dismiss).

## Cloudflare provenance rule

When `finding.enrichment.threat_indicators` contains Cloudforce One hits,
treat them as ground-truth indicators observed at the edge: cite them with
`source='cloudforce_one'` and the STIX `confidence` they carry, and do not
downgrade them against a weaker third-party source. When
`finding.evidence.cloudy_summary` is present, it is premium per-event
context: quote it with its provenance, do not paraphrase it as your own
analysis.

## Output shape

Always finish with this report, whatever tools were or were not available.
One block per external indicator, the indicator echoed exactly as extracted:

```
indicator: 185.220.101.5
type: ip
source: cloudforce_one — c2, tor-exit; confidence: 85 (STIX)
source: recall_entity — no prior sightings; confidence: n/a
source: cf_lookup_ip_threat — not queried
recommendation: hunt
```

Close with the list of indicators to hunt and the confidence of any
attribution, labelled as your own where no source supplied one.
