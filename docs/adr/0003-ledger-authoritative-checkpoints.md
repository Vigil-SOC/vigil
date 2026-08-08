# 3. The ledger is authoritative for checkpoints; approval_actions is a mirror

Date: 2026-08-07

## Status

Accepted

## Context

A run parks at a checkpoint and waits for a human. Vigil already has a human
approval surface: `approval_actions`, `approval_service`, a UI, notifications
and a Slack path. Reusing it is the obvious move.

`docs/plan.md:343` ruled against exactly that, on the grounds that routing
control checkpoints through `approval_service` breaks the rule that the
ledger is authoritative. The hunt implementation followed that ruling with a
CLI and a file-backed inbox — adequate for a harness-owned CLI, inadequate
once analysts are expected to answer checkpoints from the product.

The tension is real in both directions. Ledger-only means new API and new
frontend before anyone can answer a checkpoint outside a terminal, and no
reuse of a notification path that already works. Approval-only means "was
this approved" has two answers, and a run whose approval exists in one store
and whose resolution does not exist in the other is no longer replayable from
its own ledger.

## Decision

The checkpoint and its resolution are ledger events. **The resolution event is
what unblocks the run** — nothing else does.

A pending checkpoint is additionally written to `approval_actions` so the
existing UI, notifications and Slack path light up. That row is a
notification artifact. An answer given through it is appended to the ledger
as a resolution event before it takes effect, and if the two stores ever
disagree, the ledger wins.

This applies to **control** checkpoints — start approval, verdict review,
scope extension, budget anomaly. Real actuation against production systems
(containment, blocking, anything that mutates a customer environment) stays
on `approval_service` as the authority, per `plan.md:343`. The harness must
never become a second ungoverned actuator.

## Consequences

Replay stays self-contained: folding a ledger reproduces every checkpoint,
who answered it and what they said, with no join against another table.

The mirror is outbound-only and derived, so it can be rebuilt from the ledger
if it drifts, and a failure to write it degrades notification rather than
correctness. It is nonetheless a second write that must be kept correct, and
the temptation to read from it will recur — a reviewer should treat any read
of `approval_actions` in the AI layer's control path as a defect.

Reusing `approval_actions` means TypeScript writes a table Python's services
also write. The rows are distinguishable by their action type, and vigil's
existing resume hook only fires for rows carrying a `workflow_run_id`, which
these do not — so it will not fire spuriously. That behaviour is load-bearing
and should be covered by a test rather than left as an observation.

## Alternatives considered

**Ledger-native only, with a new API and UI surface.** Cleanest invariant,
zero mirror. Rejected for this phase only because it blocks answering a
checkpoint anywhere but the CLI until frontend work lands; it remains the
better end state if the mirror becomes a maintenance burden.

**Route control checkpoints through `approval_service`.** One human surface
for everything. Rejected: it creates a race in which an approval row exists
and the ledger has no resolution, which makes replay of that run depend on a
second store.
