import { recalledNotesOf } from "../../contracts/memory.js";
import { canonical } from "../../core/context.js";
import { digestOf } from "./config.js";
import { buildDigest } from "./digest.js";
import { fold, type HuntEvent as LedgerEvent } from "./ledger.js";
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
  // The recalled rows the run opened on, rendered from its own recall event. A
  // Replay is both halves of what a decision was shown -- the digest and the
  // recalled prefix -- so this is rebuilt here rather than left to the caller.
  // Read off the log and nowhere else: asking a Memory would answer from a
  // neighbourhood that has moved since the run, which passes as a test and then
  // shows a decision something it never saw. Empty when the run never read
  // memory, and empty when the read could not be served.
  recalled: readonly string[];
}

// Key order is not drift: the ledger's JSONB column hands the recorded digest
// back with its keys reordered, and comparing raw bytes read every decision in
// the store as a mismatch.
const same = (a: unknown, b: unknown): boolean => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));

// The two transforms the controller applies to a built digest before presenting
// it — a rejection note and an EXPAND — only ever append to these.
function isPrefix(rebuilt: readonly unknown[], recorded: readonly unknown[]): boolean {
  return rebuilt.length <= recorded.length && rebuilt.every((item, index) => same(item, recorded[index]));
}

// Where the digest for this iteration was built, on a ledger written before
// digest_seq: the state at the end of the previous iteration, which ends where
function inferredSeq(log: readonly LedgerEvent[], iteration: number, decisionIndex: number): number {
  const first = log.findIndex((event) => event.kind === "dispatch" && event.payload.iteration === iteration);
  return first === -1 ? decisionIndex : first;
}

function differs(rebuilt: Digest, recorded: Digest): string | null {
  // Prefix rather than equality: a rejection note and an EXPAND are carried, not re-derived.
  if (!isPrefix(rebuilt.notes, recorded.notes)) return "notes are not an extension of the rebuilt digest";
  if (!isPrefix(rebuilt.expansions, recorded.expansions)) return "expansions are not an extension of the rebuilt digest";

  const body = ({ notes, expansions, ...rest }: Digest): unknown => rest;
  return same(body(rebuilt), body(recorded)) ? null : "rebuilt digest differs from the one presented";
}

// Folds the ledger up to each decision, rebuilds the digest that decision was
// made against, and checks it against the one journaled at the time. The recalled
// rows every decision in the run was shown are rebuilt alongside them.
export function replay(log: readonly LedgerEvent[]): ReplayReport {
  const projection = fold(log);
  const decisions: ReplayedDecision[] = [];

  for (const [index, event] of log.entries()) {
    if (event.kind !== "decision") continue;
    const record: DecisionRecord = event.payload;
    const exact = record.digest_seq !== undefined;
    const seq = record.digest_seq ?? inferredSeq(log, record.iteration, index);

    const rebuilt = buildDigest(fold(log.slice(0, seq)), record.iteration, digestOf(projection.hunt.spec));
    decisions.push({
      decision_id: record.decision_id,
      iteration: record.iteration,
      action: record.decision.action,
      target: record.decision.target_hypothesis_id ?? record.decision.target_entity ?? null,
      cost_usd: record.cost_usd,
      exact,
      rebuilt,
      recorded: record.digest_presented,
      mismatch: differs(rebuilt, record.digest_presented),
    });
  }

  return {
    hunt_id: projection.hunt.hunt_id,
    recalled: recalledNotesOf(log),
    decisions,
    reproduced: decisions.filter((decision) => decision.mismatch === null).length,
    inexact: decisions.filter((decision) => !decision.exact).length,
  };
}
