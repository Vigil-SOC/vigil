import type { ResolutionPayload, RunOutcome, TerminalHandoff } from "../../contracts/events.js";
import { httpAnswers } from "../../core/answers.js";
import { errorFields, logger } from "../../core/log.js";

const log = logger("agent.mirror");

// Where a run's progress goes for a human to read, and where a human's answer
// comes back from. The ledger stays the record; this is the copy the UI reads.
export interface PhaseUpdate {
  phase_id: string;
  agent: string;
  name: string;
  order: number;
  status: "running" | "completed" | "failed" | "pending_approval";
  // How a gate on this step was answered. Absent means the step has no gate, so
  // the row keeps whatever it already said.
  approval_state?: "approved" | "rejected";
  output?: Record<string, unknown>;
  error?: string;
  checkpoint_id?: string;
  question?: string;
}

// How a run ended, as the console records it. summary is the deliverable, cost is
// what it took, and handoffs are the case files it wants someone else to pick up.
export interface TerminalResult {
  outcome: RunOutcome;
  reason: string;
  summary: string;
  cost_usd?: number;
  handoffs?: TerminalHandoff[];
}

// decisions is the only inbound direction: a human answers over there, and this
// side journals what comes back, so the ledger keeps its single writer.
export interface Mirror {
  // Whether a human can answer through this one. Losing progress does not change
  // a run; a gate nobody can see or answer stops it for good.
  readonly answerable: boolean;
  phase(runId: string, update: PhaseUpdate): Promise<void>;
  terminal(runId: string, result: TerminalResult): Promise<void>;
  // A case handed to IR mid-run, filed the moment it is journaled rather than
  // held back until the run ends. A hunt escalates and keeps hunting, so its
  // terminal can be an hour of parking away -- or never arrive, if the run parks
  // for good. The terminal still carries the same handoffs, so the backend that
  // takes this must be idempotent per case.
  //
  // The one update that answers whether it landed, because it is the one whose
  // failure the terminal cannot cover: a run that parks for good writes no
  // terminal, so a push nobody retries is a case IR never receives. false means
  // "not filed, ask again"; the caller decides when to stop.
  handoff(runId: string, handoff: TerminalHandoff): Promise<boolean>;
  decisions(runId: string): Promise<ResolutionPayload[]>;
}

// A run whose progress nobody is watching still runs. Used by tests and by any
// deployment with no backend to mirror into.
export const nullMirror: Mirror = {
  answerable: false,
  phase: async () => {},
  terminal: async () => {},
  // true, not false: there is nowhere to file to, so the escalation is as filed as
  // it will ever be. Reporting failure here would have a caller that retries on it
  // re-asking a no-op on every iteration for the life of the run.
  handoff: async () => true,
  decisions: async () => [],
};

export interface MirrorOptions {
  url: string;
  token: string;
  fetch?: typeof globalThis.fetch;
}

export function httpMirror(options: MirrorOptions): Mirror {
  const call = options.fetch ?? globalThis.fetch;
  const base = options.url.replace(/\/$/, "");
  const headers = { "content-type": "application/json", authorization: `Bearer ${options.token}` };

  // Progress is not the run. A backend that cannot take an update must not fail
  // the phase that produced it, so this reports and carries on -- and answers
  // whether it landed, for the one caller that has to know.
  const post = async (path: string, body: unknown): Promise<boolean> => {
    try {
      const response = await call(`${base}${path}`, { method: "POST", headers, body: JSON.stringify(body) });
      if (!response.ok) log.warn("mirror was refused", { path, status: response.status });
      return response.ok;
    } catch (error) {
      log.warn("mirror failed", { path, ...errorFields(error) });
      return false;
    }
  };

  return {
    answerable: true,
    // Discarded here: a phase or a terminal that did not land has no second chance
    // to take, and the ledger is the record either way.
    phase: async (runId, update) => void (await post(`/${encodeURIComponent(runId)}/phases`, update)),
    terminal: async (runId, result) => void (await post(`/${encodeURIComponent(runId)}/terminal`, result)),
    handoff: (runId, handoff) => post(`/${encodeURIComponent(runId)}/handoff`, handoff),
    decisions: httpAnswers(options),
  };
}
