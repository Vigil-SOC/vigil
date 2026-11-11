---
name: queue
description: Show the open work in Vigil - findings waiting for triage and open cases. Use when asked what is in the queue, what needs looking at, or what is open.
---

# Queue

Show the analyst the open findings and the open cases in their Vigil, newest and most severe first.

Vigil's kernel does the work. This session starts it, steers it and reads it: it does not reason through a workflow itself, and it never acts on Vigil except through the `vigil` MCP tools.

Use `list_findings` and `list_cases`. Narrow by severity, status or time when the analyst asks. Keep it to a short table per kind: id, title, severity, status and age. Offer to open a case with `/vigil:case` or start an investigation with `/vigil:investigate`. Report a refusal or error as given.
