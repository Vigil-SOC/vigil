import type { AgentEvent, CheckpointPayload, RunKind } from "../contracts/events.js";
import { errorFields, logger } from "./log.js";
import type { State } from "./seams.js";

const log = logger("agent.checkpoints");

// A checkpoint is on the ledger the moment it is raised, and that is what parks
// the run. This is how a human gets told one is waiting; the ledger stays the record.
export type Announce = (runId: string, runKind: RunKind, payload: CheckpointPayload) => Promise<void>;

// Nobody to tell. The run still parks -- it simply waits for an answer that has to
// arrive some other way, which is the honest outcome rather than proceeding.
export const noAnnounce: Announce = async () => {};

export interface AnnounceOptions {
  url: string;
  token: string;
  fetch?: typeof globalThis.fetch;
}

// Idempotent on the far side, keyed by checkpoint_id, because a parked run is
// looked at on every sweep and would otherwise queue the same question each time.
export function httpAnnounce(options: AnnounceOptions): Announce {
  const call = options.fetch ?? globalThis.fetch;
  const base = options.url.replace(/\/$/, "");

  return async (runId, runKind, payload) => {
    try {
      const response = await call(`${base}/${encodeURIComponent(runId)}/checkpoints`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${options.token}` },
        body: JSON.stringify({ ...payload, run_kind: runKind }),
      });
      if (!response.ok) log.warn("announcing a checkpoint was refused", { run_id: runId, checkpoint_id: payload.checkpoint_id, status: response.status });
    } catch (error) {
      // Telling someone is not the run. A backend that cannot take the notice must
      // not fail the run that parked, which is already recorded either way.
      log.warn("announcing a checkpoint failed", { run_id: runId, checkpoint_id: payload.checkpoint_id, ...errorFields(error) });
    }
  };
}

// The checkpoint a parked run is waiting on, read back off the ledger so what a
// human is shown is what the run recorded rather than a second description of it.
export async function announceOpen<K extends Record<string, unknown>>(
  state: State<K>,
  runId: string,
  runKind: RunKind,
  checkpointId: string,
  announce: Announce,
): Promise<void> {
  const events = (await state.read(runId)) as readonly AgentEvent<Record<never, never>>[];
  const raised = events.find(
    (event) => event.kind === "checkpoint" && (event.payload as CheckpointPayload).checkpoint_id === checkpointId,
  );
  if (raised === undefined) return;
  await announce(runId, runKind, raised.payload as CheckpointPayload);
}
