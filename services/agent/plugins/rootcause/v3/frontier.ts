// What a move is and when it is spent, pure. The model names every move; which moves are open on which targets
// is lead.ts's options(). These are the rules it reads.
import type { AgentConfig } from "./config.js";
import { islands, type GraphNode, type GraphEdge, type Graph } from "./graph.js";

export type Move = "expand" | "strengthen" | "fork" | "pivot" | "branch" | "theorize" | "test" | "connect" | "choose" | "conclude";

export interface FrontierDecision {
  move: Move;
  targetId: string;
  turn: number;
  reason: string;
  source?: string; // the sourcetype the model chose to look in (a pivot searches it instead of the boundary's)
}

// Settled: nothing more to gain from checking it again. Near-certain (>= settled) even when the evidence rule
// keeps it suspected, out of attempts, or two checks did not move it.
export function isStalled(x: GraphNode | GraphEdge, cfg: AgentConfig): boolean {
  if (x.confidence >= cfg.thresholds.settled || x.strengthenAttempts >= cfg.maxStrengthenAttempts) return true;
  // Two checks that together moved it less than stallDelta (one unmoved check once left a chain at 0.83).
  const n = x.attemptConf.length;
  return n >= 2 && Math.abs(x.confidence - x.attemptConf[n - 2]!) < cfg.stallDelta;
}

export const pivotExhausted = (m: GraphNode, cfg: AgentConfig) => (m.pivotSteps ?? 0) >= cfg.maxForwardSteps;
// Unlinked findings a connect move has not tried yet (at most this many per move).
export const MAX_CONNECT = 6;
export const toConnect = (g: Graph) => islands(g).filter((n) => (n.connectAttempts ?? 0) < 1).slice(0, MAX_CONNECT);
