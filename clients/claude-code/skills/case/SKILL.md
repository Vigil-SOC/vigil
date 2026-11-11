---
name: case
description: Open one Vigil case and its evidence - details, findings, IOCs, comments and runs. Use when the analyst names a case id or asks to look at a case.
argument-hint: <case id>
---

# Case

Give the analyst one case, `$ARGUMENTS`, and its evidence as a brief.

Vigil's kernel does the work. This session starts it, steers it and reads it: it does not reason through a workflow itself, and it never acts on Vigil except through the `vigil` MCP tools.

Read it with `get_case`, `get_case_iocs` and `get_case_comments`; `get_case_tasks` and `list_runs` with the case id when tasks or past runs bear on it. Lead with what the case is about, its status and severity, then the findings, IOCs and the latest comments. Cite ids as Vigil returns them; do not invent evidence the tools did not return. If no case id was given, ask for one or offer `/vigil:queue`.

Changing the case (comments, IOCs, status, closing) goes through the case write tools only when the analyst asks for that change; each is recorded on the trail under the credential's person.
