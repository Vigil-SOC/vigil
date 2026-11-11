---
name: trail
description: Read Vigil's audit trail - who did what, when, under which principal. Use when asked what happened, who changed something, or what this session did.
argument-hint: [person, area or object]
---

# Trail

Show the analyst the part of Vigil's audit trail they ask about.

Vigil's kernel does the work. This session starts it, steers it and reads it: it does not reason through a workflow itself, and it never acts on Vigil except through the `vigil` MCP tools.

Use `list_audit_entries`, filtering by person, actor, area, object, kind or time as asked. It returns entries oldest first with a `tip`; page on `after` with the last `seq` read to get newer ones. Summarise as a short timeline: time, who (person and actor), what, on which object, outcome.

Reading the trail needs `audit.read`, which Admin, Manager and auditor roles hold. An analyst's credential is refused; report that refusal as the answer and say a person with one of those roles can read it.
