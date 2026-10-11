import type { RunKind } from "../../contracts/events.js";
import type { Claim, Leases } from "../../core/leases.js";

interface Held {
  run_kind: RunKind;
  owner: string | null;
  claim_until: number;
}

// The Leases port without Postgres, so a resume test needs no database. This
// process's clock stands in for the store's, as InProcessState does for events.
export class InProcessLeases implements Leases {
  private readonly runs = new Map<string, Held>();

  constructor(private readonly now: () => number = Date.now) {}

  async claim(runId: string, runKind: RunKind, owner: string, ttlMs: number): Promise<boolean> {
    const held = this.runs.get(runId);
    const driven = held !== undefined && held.owner !== null && held.claim_until > this.now();
    if (driven) return false;
    this.runs.set(runId, { run_kind: runKind, owner, claim_until: this.now() + ttlMs });
    return true;
  }

  async renew(runId: string, owner: string, ttlMs: number): Promise<boolean> {
    const held = this.runs.get(runId);
    if (held === undefined || held.owner !== owner) return false;
    held.claim_until = this.now() + ttlMs;
    return true;
  }

  async release(runId: string, owner: string, afterMs: number): Promise<void> {
    const held = this.runs.get(runId);
    if (held === undefined || held.owner !== owner) return;
    this.runs.set(runId, { ...held, owner: null, claim_until: this.now() + afterMs });
  }

  // Only a run still waiting: a held one is being worked on already, and moving its
  // deadline back would declare a live worker dead.
  async wake(runId: string): Promise<void> {
    const held = this.runs.get(runId);
    if (held === undefined || held.owner !== null) return;
    held.claim_until = this.now();
  }

  async finish(runId: string): Promise<void> {
    this.runs.delete(runId);
  }

  // Due at or before now, not strictly before: with no transaction to advance the
  // clock, a wake() setting this instant would be invisible to a sweep in it.
  async sweep(ttlMs: number, limit: number): Promise<Claim[]> {
    const due = [...this.runs.entries()]
      .filter(([, held]) => held.claim_until <= this.now())
      .sort((left, right) => left[1].claim_until - right[1].claim_until)
      .slice(0, limit);

    for (const [runId, held] of due) {
      this.runs.set(runId, { ...held, owner: null, claim_until: this.now() + ttlMs });
    }
    return due.map(([run_id, held]) => ({ run_id, run_kind: held.run_kind }));
  }
}
