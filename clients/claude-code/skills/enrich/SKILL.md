---
name: enrich
description: Build context around one Vigil finding or entity (host, user, IP) from data already in Vigil. Read-only; no external lookups unless the user opts in.
argument-hint: "<finding-id | entity>"
disable-model-invocation: true
allowed-tools: mcp__plugin_vigil-defender_vigil__get_finding mcp__plugin_vigil-defender_vigil__get_entity_facts mcp__plugin_vigil-defender_vigil__list_findings mcp__plugin_vigil-defender_vigil__list_cases mcp__plugin_vigil-defender_vigil__get_case
---

# Enrich a finding or entity

Target: `$ARGUMENTS`. Follow the `soc-context` rules.

1. If it looks like a finding ID, `get_finding`. Extract entities from `entity_context`, the MITRE predictions, anomaly score, data source and timestamp.
2. For each key entity, `get_entity_facts` (check the tool schema for its arguments) to pull what Vigil already concluded in earlier runs.
3. `list_findings` filtered by the entity's data where the schema allows, and `list_cases`, to find related findings and cases. Open a case with `get_case` only if it clearly matches.
4. Summarize: what the entity is, prior verdicts, related activity and timing, ATT&CK context, and **gaps**.

**External intelligence:** this command does not call VirusTotal, Shodan, OTX or any public service. Those lookups send the IOC to a third party. If the user wants one, ask first, name the exact value and the service, and skip it for IOCs from private incidents unless they confirm. Vigil's own integrations (if enabled on the instance) are an alternative that stays within the user's configured tooling.

Never write results back to Vigil from here; if the user wants a note on a case, propose the text and let them confirm the `update_case` call.
