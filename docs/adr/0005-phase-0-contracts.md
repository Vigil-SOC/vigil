# 5. The five Phase-0 interface contracts

Date: 2026-08-07

## Status

Accepted

## Context

`docs/migration-plan.md` §4 splits the migration into six workstreams that
proceed in parallel. Five interfaces sit on the boundaries between them, and
eleven issues (#591–#602) are blocked until those five are settled. Nothing in
`docs/` specified any of them: the migration plan named them and their
cross-stream dependency, and stopped there.

This is the one place in the migration where a wrong call is expensive to
unwind, because five workstreams build against it at once. It is therefore
written as types the compiler checks rather than as prose, and two of the five
are shaped so that the guarantee cannot be opted out of by a caller who did not
read this document.

## Decision

Five contracts, in `ai/contracts/` and `database/init/`.

### 1. The `Tool` port — `ai/contracts/tool.ts`

Consumed by WS-A (registry, dispatch), WS-C (#595, #600, #602), WS-D (#596).

An adapter author writes a `ToolAdapter` and receives `ToolBounds`. The harness
only ever holds a `RegisteredTool`, which carries an unexported brand and can
therefore be produced by nothing but `defineTool(adapter, bounds)`. `bounds` is
a required parameter of that function, so a tool without row and timeout bounds
does not exist as a value the registry can accept. This is what makes the
guarantee structural rather than a review convention.

Bounds are **verified, not applied**. An adapter that returns more rows than its
cap throws `ToolBoundsViolation`, because the wrapper truncating the result is
precisely the failure ADR-0004 documents: slicing a serialised result yields
malformed data the model cannot reason about and cannot tell has been shortened.
Capping belongs at the source; the wrapper's job is to catch an adapter that
failed to do it.

Failures are values, not exceptions, over a closed taxonomy. The taxonomy
carries the visibility-gap rule from `CONTEXT.md`: `timeout` and `unavailable`
are genuine gaps and may be recorded as findings about the environment;
`refused` and `invalid_args` are defects and must never be recorded as gaps.

`ToolResult` is structured rows rather than a string. Rendering for the model
happens at exactly one place in the harness, which is also what #601's
byte-stable assembly needs. No adapter renders for the model.

### 2. Event kinds and the `agent_events` discriminant — `ai/contracts/events.ts`

Consumed by WS-B (#591, #597), WS-D (#596), WS-F (#599), and the Python read
path in #590.

The table is renamed from the migration plan's `hunt_events` to
**`agent_events`**. It serves hunt, investigate and compose (D9), so naming it
for one workflow was wrong; renaming costs nothing before anything writes to it.

Event kinds are two-level. Eight domain-free kinds — `run`, `spend`,
`dispatch`, `checkpoint`, `resolution`, `directive`, `patch`, `terminal` —
belong to the harness. Each workflow declares its own closed set, and the ledger
repository is generic over it, so the harness never imports a workflow's
vocabulary and ADR-0002's domain-free requirement can be checked rather than
asserted. `kind` stays bare in the table, scoped by a new `run_kind` column,
which keeps #591's fold-equivalence gate over the historical ledgers a straight
comparison rather than a rewrite.

Two changes from the prototype's flat set of thirteen:

**`spend` becomes its own kind**, replacing the `cost_usd` field the prototype
hung on decision and dispatch records. The budget is then a fold over one kind
of event, identical for lead, workers, critic and enrichment. Today
`ai/llm.ts:costOf()` collapses tokens to dollars at the call site, which is why
a run cannot be re-priced and the caching work in #601 has no baseline.

**`terminal` becomes its own kind** rather than a patch to run status. This is
what lets the Python API report an outcome with one indexed query.

**The rule for adding a kind.** Add it to the domain-free set if it is
domain-free, otherwise to the workflow's. Compilation then breaks at every
exhaustive `switch` over the fold, which is the point of ADR-0001's load-bearing
argument. Adding a kind does not bump `EVENT_SCHEMA_VERSION` — old ledgers
simply never contain it; changing an existing kind's payload shape or meaning
does. It is never a migration: `kind` is `text` and is validated in TypeScript.
A kind the fold ignores is a review defect, with `finalize` the one documented
exception, because a report is a deliverable rather than state.

**The Python read path is a hard boundary.** The fold exists once, in
TypeScript. Python may run exactly two queries against `agent_events`: an
existence or `MAX(seq)` check, and the payload of the `terminal` event. Anything
richer means a second fold implementation in a second language, and the two
would drift. Richer reads go through a TypeScript-owned API in WS-F.

### 3. `Budget` and shared `Spend` — `ai/contracts/budget.ts`

Consumed by WS-A (the seam), WS-D (#596), WS-E (#593).

`reserve` / `commit` / `release`, not check-then-spend. With
`dispatch.max_workers` above one, concurrent independent checks each pass and
collectively overshoot a single pool; a reservation makes the pool the only
arbiter. That is the actual answer to how a lead, its workers and a critic draw
on one budget.

A refusal is a **value over a closed union**, never a throw. `unpriced_model`
**fails closed**: a model with no row in the rate table is refused rather than
priced at zero, because a zero rate silently disables the cost cap entirely.
This is deliberately the inverse of `services/model_registry.py`, which falls
back to `(0, 0)` and only increments an OTEL counter.

A refusal is journaled on kinds that already exist rather than on new ones. One
that stops the run parks it as a `checkpoint` of class `budget_anomaly`. One
that refuses a single call inside an iteration lands as the `failure_reason` on
the dispatch that could not run, and the question it was answering returns to
the frontier — the same path #597 uses for an interrupted dispatch.

### 4. The BullMQ job payload — `ai/contracts/job.ts`

Consumed by WS-B (#597), WS-E (#594) and the Python backend.

A discriminated union on `reason`. **The `resume` arm carries no `request`**, so
a resume path that tried to read one would not compile. That is the "resumable
from nothing but the payload plus the ledger" requirement expressed as a type
rather than as a review note.

A `start` carries references — arch, playbook, config, prompt — rather than
resolved content, because at seq 0 there is no `run` event to read a spec from
and Python must not write one (D2). The worker resolves them and journals the
resolved spec into the `run` event, after which resume needs the payload and the
ledger only. This is already how the prototype's `resumeHunt()` behaves, and it
is why editing an arch file mid-run cannot change a run in flight.

`jobId` is the `run_id` for a start, so a double POST dedupes inside BullMQ
rather than in application code, and `run_id:seq` for a re-entrant resume step,
so a double enqueue at the same ledger position dedupes for the same reason.

The queue is named `agent-runs`, with no colon. BullMQ's Node library refuses a
queue name containing one; its Python library accepts it and writes the keys
anyway. The first name tried was `agent:runs`, which enqueued cleanly from
Python and could never have been consumed by any Node worker. Redis keys are
therefore `bull:agent-runs:*`, which is what #594's autoscaler must watch.

### 5. The rate table — `database/init/20_model_rates.sql`, `ai/contracts/rates.ts`

Consumed by WS-E (#593) and WS-A.

Every rate is **USD per million tokens, here and everywhere**. The Python
registry stores per-million in its catalog and hands out per-1k on `ModelInfo`
— two units for one number in one file, which is the class of bug #593 was
filed for. The unit is stated in each column name.

**Cache rates are stored, not derived.** Python computes them from a
per-provider multiplier table; storing the four rates explicitly means the agent
layer never reimplements that table, which is the "delete the redundant
arithmetic" half of #593.

The table is read once at startup into a frozen map. The budget gate prices
against it in-loop, with no per-call gateway round-trip, because a run that
overshoots by one expensive iteration is exactly what the gate exists to
prevent. A lookup miss returns `undefined` so the budget refuses.

## Consequences

Two guarantees are now enforced by the compiler rather than by review: a tool
cannot enter the registry having opted out of its bounds, and a resume cannot
read start-time state. Both are covered by `@ts-expect-error` assertions in
`ai/tests/contracts.test.ts`, so loosening either contract fails `typecheck`
rather than silently widening the boundary.

The DDL ships as `19_agent_ledger.sql` and `20_model_rates.sql`. The migration
plan said `18_`, which was taken by `18_agent_decision_ids.sql`. Both files went
through all three steps in `database/init/README.md` — the chart bundle copy
that CI diffs, and the `values.yaml` list that it does not.

Spend moving to its own event kind means the hunt workflow's existing
`cost_usd` fields on decision and dispatch records go away in #596. That is a
re-homing cost paid once, and it is what makes a run re-priceable.

Two BullMQ libraries now sit on one queue, and nothing enforces that they
agree. They are separately versioned lines — 3.x on PyPI, 5.x on npm — so
there is no matching version number to keep them in step, only a shared key
layout and a shared set of Lua scripts. Both are therefore exact pins, both are
excluded from automated dependency updates, and the walking skeleton's
integration test is the only thing that would catch a divergence, which is why
it runs in CI rather than only locally. The Python package is additionally
classified alpha.

The harness/workflow split in the event-kind set is drawn from one workflow, so
ADR-0002's warning applies here too: expect the line between domain-free and
workflow kinds to move once, when `chat` or `investigate` lands.

## Alternatives considered

**One flat union of every event kind.** Simplest, and the most directly
exhaustive. Rejected: the harness would import hunt vocabulary, so ADR-0002's
domain-free requirement could not be checked.

**An envelope with an opaque payload the workflow parses.** The cleanest
boundary. Rejected: it discards the discriminated-union exhaustiveness that
ADR-0001 names as the load-bearing reason for choosing TypeScript at all.

**Prefixed kinds (`hunt.hypothesis`) with no `run_kind` column.** Globally
unique and unambiguous in raw SQL. Rejected: every historical ledger event would
need its kind rewritten on import, which weakens #591's fold-equivalence gate
from a comparison into a transformation.

**Check-then-spend on the budget.** One fewer call in the common path. Rejected
on fan-out: it is unsound the moment more than one worker draws on the pool.
