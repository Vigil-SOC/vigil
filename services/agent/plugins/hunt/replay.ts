import { recalledNotesOf } from "../../contracts/memory.js";
import { fold, type HuntEvent as LedgerEvent } from "./ledger.js";
import type { DecisionRecord, Digest } from "./types.js";

// Replay of a controller hunt: each decision shown with the digest it was journaled with,
// no longer rebuilt. Kernel runs are replayed call by call (core/replay.ts).

export interface ReplayedDecision {
  decision_id: string;
  iteration: number;
  action: string;
  target: string | null;
  // Null where nothing priced the decision's model call.
  cost_usd: number | null;
  // Wall time of the lead's model calls; omitted on ledgers that did not record it.
  duration_ms?: number;
  // Always false now: nothing is rebuilt, so nothing is exact.
  exact: boolean;
  // Null: the digest is shown as recorded and not rebuilt.
  rebuilt: Digest | null;
  recorded: Digest;
  mismatch: string | null;
}

export interface ReplayReport {
  hunt_id: string;
  decisions: ReplayedDecision[];
  reproduced: number;
  inexact: number;
  // The recalled rows the run opened on, from its own recall event and nowhere else: a
  // Memory would answer from a neighbourhood that has moved since.
  recalled: readonly string[];
}

export function replay(log: readonly LedgerEvent[]): ReplayReport {
  const projection = fold(log);
  const decisions: ReplayedDecision[] = log
    .filter((event): event is Extract<LedgerEvent, { kind: "decision" }> => event.kind === "decision")
    .map((event) => {
      const record: DecisionRecord = event.payload;
      return {
        decision_id: record.decision_id,
        iteration: record.iteration,
        action: record.decision.action,
        target: record.decision.target_hypothesis_id ?? record.decision.target_entity ?? null,
        cost_usd: record.cost_usd,
        ...(record.duration_ms === undefined ? {} : { duration_ms: record.duration_ms }),
        exact: false,
        rebuilt: null,
        recorded: record.digest_presented,
        mismatch: null,
      };
    });
  return { hunt_id: projection.hunt.hunt_id, recalled: recalledNotesOf(log), decisions, reproduced: 0, inexact: decisions.length };
}
