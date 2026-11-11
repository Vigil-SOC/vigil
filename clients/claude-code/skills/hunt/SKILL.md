---
name: hunt
description: Start a Vigil threat hunt on a case from a hypothesis, steer it, and read what it found once it ends. Also reads past hunts. Use when asked to hunt for something.
argument-hint: <case id> <hypothesis>
---

# Hunt

Have Vigil hunt for the hypothesis in `$ARGUMENTS` and bring back what it found.

Vigil's kernel does the work. This session starts it, steers it and reads it: it does not reason through a workflow itself, and it never acts on Vigil except through the `vigil` MCP tools.

Choose the hunt workflow from `list_workflows` (or an operator agent from `list_operator_agents`), and ask when more than one fits. A run starts on a case: use the case the analyst names, or ask whether to open one with `create_case`. Start it with `start_run`, the case id and the hypothesis, and give the analyst the run id.

Read progress with `get_run`, steer with `send_directive` only when asked, and point to `/vigil:waiting` if it pauses on an approval. Once it has ended, report its conclusion, citations and declared gaps as Vigil gave them. `list_completed_hunts` lists earlier hunts when the analyst asks what was already hunted.
