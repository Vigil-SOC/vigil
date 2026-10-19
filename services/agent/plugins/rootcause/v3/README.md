# Root cause v3

The graph-based root-cause loop, ported from the standalone RCA loop (v3, snapshot of
2026-10-16). The files keep the loop's own layout and wording so they can be read against
it. Comments that cite `spec §` refer to that loop's `rca-agent-spec.md`. No comment or
test names an evaluation scenario: tests/core/rootcause-v3-scenarios.test.ts holds that.

What the port changed:
- No files. The loop wrote graph.json, state.json, trace.jsonl, environment.md and
  report.md. Now `run` hands the graph to `onTurn` after every turn and returns the
  report, `scope` takes the map the run already holds, and the trace lives in memory
  (`MemoryTrace`).
- The model is behind `model/messages.ts`, in the Anthropic Messages shape the loop was
  tuned on: prompt caching, adaptive thinking, a forced submit tool. Whatever reaches
  the model implements `Messages.create`.
- Searches run through `tools/job.ts`'s `JobRunner`. The loop's own Splunk REST client,
  its survey discovery, the CLI, the session driver and the grader stay in the
  standalone repo.
- Schemas are zod 4.6.5, imported as `zod4` (openai@4 pins zod 3 as a peer). Nothing
  outside plugins/rootcause imports it.
- Type-checked by `tsconfig.rootcause-v3.json`, with `noUncheckedIndexedAccess` off,
  which the loop never compiled under.

Not wired to the worker yet. Nothing here runs a `root_cause` run.
