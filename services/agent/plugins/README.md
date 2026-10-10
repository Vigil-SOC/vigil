# Plugins

Every agent run is one loop, the kernel (`core/agent.ts`), driven by a plugin.
A plugin says what kind of work a run is. The kernel does everything a run needs
whatever its kind, so no plugin writes it again:

- opens the run and journals the spec it ran under, stamped `engine: plugin`
- reads episodic memory once at the start, journaled as the `recall` event
- runs the agent's tool loop (`core/stream.ts`): budgets, approvals that park the run, folding a long transcript
- journals every tool call as a `call` event the moment its result arrives, numbered `r1`, `r2`, ... and shown to the model so its answer can cite them
- on resume, rebuilds the transcript from those calls, so a park or a crash re-runs nothing
- honours an operator's stop while a call is in flight, and hands the run back untouched when the lease is lost
- ends the run with exactly one terminal

`agentProjection` and `agentReplay` (`core/agent-view.ts`) read any kernel run.

## What a plugin is

A directory: `<id>/plugin.yaml`, a `skills/` folder, and optionally `hooks.ts`.

```yaml
id: investigate
description: Work one open case to a conclusion a reader can act on.
needs: [case_records, get_finding, telemetry_search]   # capabilities, bound by each tool's `provides`
system: |
  Who the agent is and how it works.
skills: [investigate-case]          # skills/<name>/SKILL.md, loaded on demand
conclusion:                         # or `conclusion: prose`
  summary_field: summary
  citations_field: citations        # optional; numbers results so the answer can cite them
  schema: { ... }                   # JSON schema the final answer must match
guard:                              # optional second look before a guarded answer stands
  field: disposition
  guarded: [benign, false_positive]
  fallback: needs_review
  reviewer_prompt: |
    ...
max_turns: 24                       # optional; else the deployment's runtime.max_turns
```

A skill is `skills/<name>/SKILL.md` with `name` and `description` front matter and
the playbook as its body. The system prompt carries each skill's name and one
line; the body arrives only when the agent calls `load_skill`.

The guard is how a plugin makes some answers harder to give than others. When
the answer's `field` holds a guarded value, a reviewer with no tools is shown the
answer and the records it cites. The answer stands only if the reviewer upholds
it over at least one cited ref that is a real call. Otherwise the run ends on the
fallback. Refs are checked by counting, whatever the reviewer says.

## Hooks

A plugin whose work needs code ships `hooks.ts` and registers it in `index.ts`.
Every hook is optional, and the kernel calls each at a fixed point:

| Hook | When | Used by |
| :-- | :-- | :-- |
| `open` | once, writing the run event itself | hunt: its state rides in the run event |
| `tools` | each pick-up: tools that answer in this process | hunt's record, root cause's `record`/`finish`, tally's `bump` |
| `dispatch` | wraps how a call reaches its tool | root cause trims what a search shows |
| `turns` | the run's tool-turn allowance | hunt: its budget, which an operator can extend |
| `before` | each pick-up, before the loop: park or end here | hunt directives and checkpoints, root cause's permit, compose's whole phase walk |
| `brief` | what the run is about | hunt's board, root cause's steps so far |
| `observe` | every loop event, after the kernel journals its own: may `"halt"` or park | hunt's gaps and escalations, tally's target |
| `finish` | the loop answered or was refused: how the run ends, or parks | hunt's ceiling, root cause's delivery |
| `halted` | after an observer halted | hunt, tally |
| `exhausted` | the gateway's own allowance refused | hunt |
| `stopped` | an operator stopped it: what it leaves behind | root cause, hunt |
| `ended` | after the terminal is written | compose reports to the console mirror |

`runSubturn` runs one turn under a role, prompt and grants of the plugin's
choosing, journaling its calls under that role. It is for work that is an
authored sequence rather than one agent's judgement. Compose's phases use it.

## The plugins

| Plugin | Run kind | Notes |
| :-- | :-- | :-- |
| `investigate` | investigate | Files only. The dismissal guard holds `benign`/`false_positive` for review. |
| `hunt` | hunt | One agent keeps the hunt's record through its tools, in `book.ts`. The record is the controller's, unchanged, so the projection, the distil episodic memory reads and the report fold it as before. |
| `adjudicate` | adjudicate | Same hooks as hunt, without escalation. |
| `rootcause` | root_cause | The proof is code (`workflows/rootcause/proof.ts`); the model never says a link is proved. |
| `compose` | compose | Walks the playbook's authored phases in order, each a subturn under its own agent and grants. |
| `chat` | chat | A conversation: the caller holds the transcript and nothing writes a terminal. |
| `tally` | tally | The conformance plugin. If a plugin this small needs the kernel to know anything about it, the boundary is in the wrong place. |

## Adding one

1. Write `<id>/plugin.yaml` and its skills.
2. If it needs code, write `<id>/hooks.ts` and register it in `index.ts`.
3. Give its run kind an entry in `arch/registry.ts`, and a projection if the console reads it in a shape of its own.
4. Test it with a scripted model that answers what it is shown. See `tests/hunt/kernel-hunt.test.ts`.
