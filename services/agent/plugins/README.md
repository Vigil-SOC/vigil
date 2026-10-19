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

And the five things a run needs to be trusted and learned from:

- **The hypothesis record** (`core/record.ts`). `propose_hypothesis`, `link_evidence` (supports or weakens) and `open_question`, offered to every plugin and journaled as `hypothesis`, `link` and `question` events. A plugin with a richer record supplies its own backend through the `record` hook, writing the same kinds in its own shape (the hunt's carry its verdict machinery). `record: required` also holds a guarded answer to the record: it stands only on a cited, served result the record links to a claim.
- **A record that costs few calls.** `link_evidence` can propose a hypothesis by statement in the same call, and every agent that keeps a record is told to write it in the same turn as its next query: a record write in a turn of its own is a model call that looked at nothing.
- **Declared gaps.** Every conclusion carries `declared_gaps`, computed off the ledger: the record's open questions, the answer's own `open_questions_field`, and every call the estate could not answer.
- **Classified results.** Every call is `served`, a `visibility_gap` (timeout, unavailable, backend error) or a `defect` (refused, invalid arguments). Only gaps are gaps. A defect or a gap is refused as evidence.
- **A frame per model call** (`core/frames.ts`). Before each call: hashes of the system prompt, task, recall block, each tool schema, each skill body, the emission ask and schema, the model, its sampling parameters, the fold policy and how much it folded, and the refs of the calls in the transcript. Each hash's bytes go on the ledger once, as a `content` event.
- **Judges and subagents as child runs.** `runChild` runs one turn on a ledger of its own (`run_kind: subagent`), its run event naming its parent, the parent writing a `child` event when it ends. Its spend is the parent's. The kernel's reviewer, the hunt's critic and its narrator run this way. A plugin's authored steps (`runSubturn`) stay on the parent's ledger under their own role, because their approvals are the run's.

`agentProjection` and `agentReplay` (`core/agent-view.ts`) read any kernel run. Replay (`core/replay.ts`) rebuilds every frame's input from the ledger alone and diffs its hash against the one sent; `trajectoryOf` hands a learner the same steps, from the ledger alone. Nothing either reads calls a tool or reads memory.

## What a plugin is

A directory, `plugins/<id>/`, holding everything about one kind of run:

- `plugin.yaml`: what the agent is, what it may use, how it ends (below), and the run's roles: what each may use, and for the hunt its critic, the shape a finding takes (`finding`) and what an iteration may spend (`allowance`). This is the arch layer of the arch · playbook · config spec; `plugins/kinds.ts` maps each run kind to its plugin and to the views below
- `hooks.ts`, optionally: code the kernel calls at its seams
- the plugin's own domain: its ledger vocabulary, the console view (`view.ts`), memory distil and recall, and readers for ledgers an earlier engine wrote (`investigate/legacy/`, `hunt/projection.ts`)

Skills are not in the plugin. They live in the repo's one library, `core/skills/library/<name>/SKILL.md`, shared with the Python agents and the console; a plugin names the ones it uses.

```yaml
id: investigate
description: Work one open case to a conclusion a reader can act on.
needs: [case_records, get_finding, telemetry_search]   # capabilities, bound by each tool's `provides`
system: |
  Who the agent is and how it works.
skills: [investigate-case]          # core/skills/library/<name>/SKILL.md, loaded on demand
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
record: required                    # off | available (the default) | required
min_results: 4                      # optional; see below
```

`min_results`: a run that stops calling tools with turns left and fewer than this
many results from the estate (playbooks and record writes do not count) is asked
once to keep going or confirm it is done. The ask is the next call's transient
tail, journaled on its frame, so the run still replays exactly.

A required record needs a structured conclusion that names an `open_questions_field` its schema requires; the manifest is refused at load otherwise.

A skill is `skills/<name>/SKILL.md` with `name` and `description` front matter and
the playbook as its body. The system prompt carries each skill's name and one
line; the body arrives only when the agent calls `load_skill`.

The guard is how a plugin makes some answers harder to give than others. The reviewer sees every cited record, then the uncited ones newest first up to a bounded window, and never a loaded playbook: the cited records are what the answer stands on, and an unbounded window was the reviewer's whole cost. When
the answer's `field` holds a guarded value, a reviewer with no tools is shown the
answer and the records it cites. The answer stands only if the reviewer upholds
it over at least one cited ref that is a real call. Otherwise the run ends on the
fallback. Refs are checked by counting, whatever the reviewer says.

`quarantine: { reader, guidance }` (experimental, `core/guards.ts`) is a different thing from `guard`. It is honoured only with `VIGIL_GUARDS=on`, and today only by hunt: every estate result goes to a tool-less reader child run, and the deciding agent sees its typed findings, with text the subject could have written reaching it only as a handle. The raw rows stay on the ledger.

## Hooks

A plugin whose work needs code ships `hooks.ts` and registers it in `index.ts`.
Every hook is optional, and the kernel calls each at a fixed point:

| Hook | When | Used by |
| :-- | :-- | :-- |
| `open` | once, writing the run event itself | hunt: its state rides in the run event |
| `tools` | each pick-up: tools that answer in this process | hunt's record, root cause's `record`/`finish`, tally's `bump` |
| `dispatch` | wraps how a call reaches its tool | root cause trims what a search shows |
| `record` | where the record's tools write | hunt: its board, rulings and frontier |
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
| `rootcause` | root_cause | The proof is code (`plugins/rootcause/proof.ts`); the model never says a link is proved. |
| `compose` | compose | Walks the playbook's authored phases in order, each a subturn under its own agent and grants. |
| `chat` | chat | A conversation: the caller holds the transcript and nothing writes a terminal. |
| `tally` | tally | The conformance plugin. If a plugin this small needs the kernel to know anything about it, the boundary is in the wrong place. |

## Adding one

1. Write `<id>/plugin.yaml` and its skills.
2. If it needs code, write `<id>/hooks.ts` and register it in `index.ts`.
3. Give its run kind an entry in `plugins/kinds.ts`, and a projection if the console reads it in a shape of its own.
4. Test it with a scripted model that answers what it is shown. See `tests/hunt/kernel-hunt.test.ts`.
