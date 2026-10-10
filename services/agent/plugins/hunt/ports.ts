import type { Narrative } from "./narrative.js";
import type { Directive, NullCheckInput, NullCheckResult } from "./types.js";

// Where a directive waits before the run takes it. Any process enqueues, only the
// run drains, and nothing is deleted on read: what is journaled is the ledger's fact.
export interface DirectiveQueue {
  enqueue(runId: string, directive: Directive): Promise<void>;
  pending(runId: string, journaled: readonly string[]): Promise<Directive[]>;
}

// Argues the strongest benign explanation against a hypothesis before it may be
// proven. It returns its argument; the hunt plugin decides what to write.
export interface DisconfirmationCritic {
  argueNull(check: NullCheckInput): Promise<NullCheckResult>;
}

// Writes the account a person reads first, from the hunt's own record. Returns it
// rather than journaling it: the hunt plugin decides what lands on the ledger.
export interface Narrator {
  narrate(input: string): Promise<Narrative>;
}
