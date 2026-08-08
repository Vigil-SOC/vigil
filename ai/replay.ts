import { buildDigest } from "./digest.js";
import { fold, type LedgerEvent } from "./ledger.js";
import type { DecisionRecord, Digest } from "./types.js";

export interface ReplayedDecision {
  decision_id: string;
  iteration: number;
  action: string;
  target: string | null;
  cost_usd: number;
  // False when the ledger predates digest_seq and the prefix had to be inferred,
  // so a mismatch there may be the boundary rather than real drift.
  exact: boolean;
  rebuilt: Digest;
  recorded: Digest;
  mismatch: string | null;
}

export interface ReplayReport {
  hunt_id: string;
  decisions: ReplayedDecision[];
  reproduced: number;
  inexact: number;
}

// The two transforms the controller applies to a built digest before presenting
// it — a rejection note and an EXPAND — only ever append to these.
function isPrefix(rebuilt: readonly unknown[], recorded: readonly unknown[]): boolean {
  return (
    rebuilt.length <= recorded.length &&
    rebuilt.every((item, index) => JSON.stringify(item) === JSON.stringify(recorded[index]))
  );
}

// Where the digest for this iteration was built, on a ledger written before
// digest_seq: the state at the end of the previous iteration, which ends where
// this iteration's first dispatch was journaled.
function inferredSeq(log: readonly LedgerEvent[], iteration: number, decisionIndex: number): number {
  const first = log.findIndex((event) => event.kind === "dispatch" && event.dispatch.iteration === iteration);
  return first === -1 ? decisionIndex : first;
}

function differs(rebuilt: Digest, recorded: Digest): string | null {
  // Prefix rather than equality: a rejection note and an EXPAND are carried, not re-derived.
  if (!isPrefix(rebuilt.notes, recorded.notes)) return "notes are not an extension of the rebuilt digest";
  if (!isPrefix(rebuilt.expansions, recorded.expansions)) return "expansions are not an extension of the rebuilt digest";

  const body = ({ notes, expansions, ...rest }: Digest): string => JSON.stringify(rest);
  return body(rebuilt) === body(recorded) ? null : "rebuilt digest differs from the one presented";
}

// Folds the ledger up to each decision, rebuilds the digest that decision was
// made against, and checks it against the one journaled at the time.
export function replay(log: readonly LedgerEvent[]): ReplayReport {
  const projection = fold(log);
  const decisions: ReplayedDecision[] = [];

  for (const [index, event] of log.entries()) {
    if (event.kind !== "decision") continue;
    const record: DecisionRecord = event.decision;
    const exact = record.digest_seq !== undefined;
    const seq = record.digest_seq ?? inferredSeq(log, record.iteration, index);

    const rebuilt = buildDigest(fold(log.slice(0, seq)), record.iteration, projection.hunt.spec.digest);
    decisions.push({
      decision_id: record.decision_id,
      iteration: record.iteration,
      action: record.decision.action,
      target: record.decision.target_hypothesis_id ?? record.decision.target_entity ?? null,
      cost_usd: record.cost_usd,
      exact,
      rebuilt,
      recorded: event.digest_presented,
      mismatch: differs(rebuilt, event.digest_presented),
    });
  }

  return {
    hunt_id: projection.hunt.hunt_id,
    decisions,
    reproduced: decisions.filter((decision) => decision.mismatch === null).length,
    inexact: decisions.filter((decision) => !decision.exact).length,
  };
}
