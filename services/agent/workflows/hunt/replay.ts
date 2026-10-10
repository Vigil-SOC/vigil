import { recalledNotesOf } from "../../contracts/memory.js";
import { fold, type HuntEvent as LedgerEvent } from "./ledger.js";
import type { DecisionRecord, Digest } from "./types.js";

// Replay of a hunt the controller ran. Each decision is shown with the digest it
// was presented, as journaled. It is no longer rebuilt: the digest builder went
// with the controller, and a kernel run is replayed a different way -- every
// model call's input rebuilt from the ledger and diffed against the hash it was
// sent with (core/replay.ts). What a controller decision was shown is still
// exactly what the ledger recorded, which is what a reader of one needs.

export interface ReplayedDecision {
  decision_id: string;
  iteration: number;
  action: string;
  target: string | null;
  cost_usd: number;
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
  // The recalled rows the run opened on, rendered from its own recall event and
  // read off the log and nowhere else: asking a Memory would answer from a
  // neighbourhood that has moved since the run.
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
