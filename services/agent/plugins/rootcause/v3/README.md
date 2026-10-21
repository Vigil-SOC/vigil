# Root cause v3

The graph-based root-cause loop, ported from the standalone RCA loop (v3, snapshot of
2026-10-16). The files keep the loop's own layout and wording so they can be read against
it. Comments that cite `spec §` refer to that loop's `rca-agent-spec.md`. No comment or
test names an evaluation scenario: tests/core/rootcause-v3-scenarios.test.ts holds that.

Every `root_cause` run goes here (`worker.ts`). The kernel plugin under `plugins/rootcause/`
is unreachable from the worker and is retired separately.

How a run goes (`engine.ts`):
- It opens its own ledger and stamps the `run` event's `sections.engine` with
  `rootcause-v3`; `plugins/kinds.ts` (projection) and `serve.ts` (replay) pick their reader
  off that stamp.
- Every model call is a kernel turn: `runSubturn` through `model/client.ts`, the one
  place the loop touches the kernel. A move's role is the move, its system prompt the
  loop's prompt plus the survey, the environment map and the rare sweep, its task the
  move's prompt, its schema the move's submit schema, and its `max_turns` the move's search
  cap. Framing the brief (`frame.ts`) is a turn with the frame schema and no tools. The
  kernel bills each call to the run's pool, journals `spend`, `frame` and `call` events,
  and delimits search results; v3 keeps none of that of its own.
- The model searches through one evidence tool (`tools/`), granted as a local tool whose
  handler is the `Registry`'s `run`. The kernel shows each result with its ref (`r12`) and
  journals the search as a `call` event with it; the model cites refs. The registry keeps
  what each ref ran and returned for the baseline checks. A search the code makes itself
  (the rare sweep, a story's sweep, a presence lookup) takes the next ref and is journaled the same way.
- A move's answer is checked once, in `check()`: an accusing label with no baseline is
  tagged unverified, a link with no shared value is kept as inferred, and an item citing a
  ref the ledger never journaled is dropped. A turn with no valid answer is one attempt
  on its target. Nothing is asked again.
- `ledger.ts` journals `rootcause_setup`, `rootcause_turn` (the turn's trace entries, its
  graph patch among them) and `rootcause_exit`. The graph is folded back from patches, so
  a resume continues at the next turn with its graph, trace and calls as they were.
- An operator's stop is polled between and during turns, a lost lease leaves no terminal,
  and the pool refusing a call ends the run `budget_exhausted`.

What the port changed:
- No files. `run` hands the graph to `onTurn` after every turn and returns the report,
  `scope` takes the map the run already holds, and the trace lives in memory (`MemoryTrace`).
- Searches run through `tools/job.ts`'s `JobRunner`, fed by a `Source` (`tools/source.ts`)
  that turns the run's bound telemetry tool into the evidence tool and the survey. Splunk
  is the one backend so far (`tools/splunk-source.ts`), and the prompts' search rules are SPL.
- Schemas are hand-written JSON Schema (`model/schemas.ts`, `tools/splunk-tool.ts`) with matching
  interfaces. The agent's one ajv (`core/stream.ts`) holds an emission to them in the kernel, and
  `schemaErrors` holds a search's input (`Registry.run`), a scripted submission and a salvage to them.
- Type-checked by the package's tsconfig like the rest, `noUncheckedIndexedAccess`
  included, which the loop never compiled under.
