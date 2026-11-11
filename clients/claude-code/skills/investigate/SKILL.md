---
name: investigate
description: Start a Vigil investigation run on a case, steer it, and read its conclusion, citations and declared gaps once it ends. Use when asked to investigate a case.
argument-hint: <case id> [what to look at]
---

# Investigate

Have Vigil investigate the case in `$ARGUMENTS` and bring back what it concluded.

Vigil's kernel does the work. This session starts it, steers it and reads it: it does not reason through a workflow itself, and it never acts on Vigil except through the `vigil` MCP tools.

Choose what to start from `list_workflows` (an investigation workflow) or `list_operator_agents` (an operator agent starts as `agent-run-<id>`); ask when more than one fits. Start it with `start_run` and the case id, passing the analyst's question as `context` or `hypothesis`. Give the analyst the run id.

While it runs, `get_run` shows its status. Steer it only when the analyst asks, with `send_directive` (a note, a lead, extend, conclude or abort). If it pauses on an approval, say what it waits on and point to `/vigil:waiting`.

Read the conclusion only after the run has ended: `get_run` then returns the conclusion, disposition, citations and declared gaps. Report them as Vigil gave them, gaps included; do not fill a gap with your own reasoning. A run that ends failed or cancelled is reported with its reason.
