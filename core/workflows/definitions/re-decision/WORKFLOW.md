---
name: re-decision
version: 1
description: "Re-decide one closure made before this install, from the alert and the events still held, without the verdict that closed it."
use_case: "One past closure, investigated again on the evidence available when it was closed."
trigger_examples:
  - "Re-decide this pre-install closure from its alert"
  - "Was this closed ticket real, on the events held at the time?"
run_kind: investigate
goal: "Decide whether the activity in the alert was real, from events up to the close, without the verdict that closed it."
needs:
  - splunk-selfhosted_splunk_execute
  - elastic_elastic_search_logs
  - opensearch_opensearch_search_logs
  - amazon-security-lake_security_lake_query
  - lookup_indicators
  - recall_entity
objectives:
  - "Decide whether the activity in the alert was real"
  - "Use the alert's content and events up to the close"
  - "Leave the prior verdict unread"
---

# Re-decision

Decide whether the activity in the alert was real. The alert text in the request is the whole starting record. There is no case.

The prior disposition is not in that text and is not evidence. Do not look for it, and do not treat a remembered conclusion about these entities as the answer you are here to give.

Searches are held to the close. A Splunk query that sets `latest` after the close is refused. Ask for the window you mean; the bound is applied for you. An empty search is a gap, not a benign result.

Indicator lookups answer as of now. Say so when one bears on the answer.

Conclude with the disposition the answer asks for. Containment you would recommend goes in `proposed_actions` and stops there: you do not take it.
