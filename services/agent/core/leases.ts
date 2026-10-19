import type { RunKind } from "../contracts/events.js";

// Not one of the four seams: a turn has no business renewing a lease. It belongs
// to whatever drives a run, and to the sweeper that reclaims one nobody drives.

// Deployment timings, so a slow cluster widens them without a rebuild. Not in
// core/config.py: nothing in this layer reads Python's settings surface.
function ms(name: string, fallback: number): number {
  const raw = Number(process.env[name]);
  return Number.isFinite(raw) && raw > 0 ? raw : fallback;
}

// How long a claim lasts and how often its holder pushes it forward. 4x, not 3x:
// renewal shares a loop with the fold, and a live worker declared dead pays twice.
export const LEASE_TTL_MS = ms("VIGIL_LEASE_TTL_MS", 60_000);
export const RENEW_EVERY_MS = ms("VIGIL_LEASE_RENEW_MS", 15_000);

// How often the sweeper looks, and how long a parked run waits to be looked at.
// The interval is the safety net once the console can pull a run forward.
export const SWEEP_EVERY_MS = ms("VIGIL_SWEEP_MS", 30_000);
export const PARK_EVERY_MS = ms("VIGIL_PARK_POLL_MS", 60_000);

// A run the sweeper reserved and must now hand to the queue. run_kind is here
// because a resume job needs it and the ledger read would be a second query.
export interface Claim {
  run_id: string;
  run_kind: RunKind;
}

export interface Leases {
  // False when someone else is driving it. A null owner is nobody driving -- reserved
  // or parked -- so the job's holder may take it. Refusal is the good case, not a failure.
  claim(runId: string, runKind: RunKind, owner: string, ttlMs: number): Promise<boolean>;
  // False when the claim was stolen, which tells the holder it has been declared
  // dead and should stop paying for work nobody will record.
  renew(runId: string, owner: string, ttlMs: number): Promise<boolean>;
  // The run parked. The row stays -- it is unfinished -- and nothing touches it
  // until the interval passes or the console pulls it forward.
  release(runId: string, owner: string, afterMs: number): Promise<void>;
  // Something arrived for a parked run, so the next sweep takes it rather than
  // waiting out the interval. No owner to match: the caller never held it.
  wake(runId: string): Promise<void>;
  // The run journaled its terminal, so it is nobody's work now.
  finish(runId: string): Promise<void>;
  // Discovery and reservation in one statement, so N sweepers cannot enqueue one run
  // twice. Owner stays null: the sweeper queues the run, it does not drive it.
  sweep(ttlMs: number, limit: number): Promise<Claim[]>;
}
