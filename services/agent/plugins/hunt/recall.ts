import type { Notes } from "../../core/memory.js";
import type { State } from "../../core/seams.js";
import { fold, LedgerError, type HuntKinds } from "./ledger.js";
import type { Hypothesis } from "./types.js";

// Settled first, then the rest: a follow-up asks about what the hunt concluded far
// more often than about what it left open, and the limit cuts from the bottom.
const SETTLED = ["handed_off", "proven", "disproven"];

// What a later conversation needs from a finished hunt: what it was, how it ended,
// where each hypothesis landed. Not the evidence -- a follow-up queries for that.
export function huntNotes(state: State<HuntKinds>, runId: string): Notes {
  return async (limit) => {
    const events = await state.read(runId);
    if (events.length === 0) return [];

    try {
      const { hunt, hypotheses } = fold(events);
      const ranked = [...hypotheses.values()].sort((left, right) => rank(left) - rank(right));
      return [ended(hunt.name, hunt.outcome, hunt.termination_reason), ...ranked.map(where)].slice(0, limit);
    } catch (error) {
      // A parent whose ledger cannot be folded recalls nothing rather than
      // failing the conversation that referred to it.
      if (error instanceof LedgerError) return [];
      throw error;
    }
  };
}

function ended(name: string, outcome: string | null, reason: string | null): string {
  const how = outcome === null ? "is still running" : `ended ${outcome}`;
  return `The hunt "${name}" ${how}${reason ? `: ${reason}` : ""}.`;
}

function where(hypothesis: Hypothesis): string {
  const { statement, status, resolution_reason: why } = hypothesis;
  return `Hypothesis "${statement}" is ${status}${why ? ` -- ${why}` : ""}.`;
}

function rank(hypothesis: Hypothesis): number {
  const at = SETTLED.indexOf(hypothesis.status);
  return at === -1 ? SETTLED.length : at;
}
