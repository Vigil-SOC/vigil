---
name: waiting
description: Show what in Vigil is waiting on a person - pending approvals and the runs paused on them - and reject one if asked. Use when asked what needs approval or what a run waits on.
argument-hint: [run id]
---

# Waiting

Show the analyst what waits on a person: pending approval actions and the runs paused on them.

Vigil's kernel does the work. This session starts it, steers it and reads it: it does not reason through a workflow itself, and it never acts on Vigil except through the `vigil` MCP tools.

`get_run` on a paused run lists the approval ids it waits on; `list_approval_actions` lists pending actions across Vigil, and `get_approval_action` reads one in full. Say for each what it would do, to what, who or what proposed it, and why.

This client cannot approve. `approve_action` over MCP leaves the action pending, by design: approving happens in Vigil's Approvals screen, by a signed-in person. Say so plainly rather than retrying. Rejecting is allowed: use `reject_action` with the analyst's reason, only when they ask.
