import type { AgentEvent } from "../../../contracts/events.js";
import { resultCountIn } from "../../../core/call-view.js";
import type { AgentConfig } from "./config.js";
import type { Move } from "./frontier.js";
import { applyPatch, createGraph, getChildren, type Graph, type GraphPatch } from "./graph.js";
import { seedFromAlert } from "./orchestrator.js";
import type { Survey } from "./survey.js";
import type { TraceEntry } from "./trace.js";
import type { Alert, ExitReason } from "./types.js";

// What a v3 run journals beyond the kernel's own kinds. The graph is never stored: it is
// folded back from the alert, the rare sweep and each turn's patch, which applyPatch
// replays exactly under the journaled config. A run's patches come to a few hundred KB
// where a graph per turn would be megabytes.

export const ENGINE = "rootcause-v3";

export interface SetupPayload {
  cfg: AgentConfig;
  alert: Alert;
  index: string;
  range: [string, string];
  survey: Survey;
  // The environment map (scope.ts), or null when mapping failed and the run went on without it.
  environment: string | null;
}

export interface TurnPayload {
  turn: number;
  // Turn 0 is the rare sweep; every later turn carries its graph-patch among its entries.
  rare?: string;
  entries: TraceEntry[];
}

export interface ExitPayload {
  exit: ExitReason | null; // null: stopped or failed before an exit was reached
  report: string;
  entries: TraceEntry[];
}

export type V3Kinds = {
  rootcause_setup: SetupPayload;
  rootcause_turn: TurnPayload;
  rootcause_exit: ExitPayload;
};

// Which engine wrote a root_cause ledger, as its run event was stamped.
export function engineOf(events: readonly { kind: string; payload: unknown }[]): string | null {
  const opened = events.find((event) => event.kind === "run");
  const engine = (opened?.payload as { spec?: { sections?: Record<string, unknown> } } | undefined)?.spec?.sections?.["engine"];
  return typeof engine === "string" ? engine : null;
}

// One move of the run, as a reader follows it.
export interface V3Step {
  n: number;
  move: Move;
  targetId: string;
  reason: string;
  // failed: the search never answered, refused by the loop's own checks or by the backend; error says why.
  queries: { ref: string; query: string; rows?: number; purpose?: string; failed?: true; error?: string }[];
  added: string[];
  changed: { id: string; status: string; confidence: number; from: number; from_status: string }[];
  // What the turn did, for a reader scanning moves; all of it read from the patch and the graph the turn left.
  target: string; // the target's label, "" when none is known
  created: { id: string; kind: "cause" | "event" | "actor" | "boundary" | "link"; label: string }[];
  prediction?: { id: string; status: string }; // a prediction this turn settled
  theories?: number; // stories a theorize turn proposed
  concluded?: true;
}

export interface Folded {
  setup: SetupPayload | null;
  graph: Graph | null;
  entries: TraceEntry[];
  // The last turn journaled whole: a resume starts at the one after it.
  lastTurn: number;
  exit: ExitReason | null;
  report: string | null;
  steps: V3Step[];
}

function stepOf(turn: number, entries: readonly TraceEntry[]): V3Step | null {
  const decision = entries.find((e) => e.type === "decision")?.payload as { move: Move; targetId: string; reason?: string } | undefined;
  if (decision === undefined) return null;
  const outputs = new Map<string, { ok: boolean; text: string }>();
  for (const e of entries) {
    if (e.type !== "tool-output") continue;
    const p = e.payload as { toolCallId: string; ok?: boolean; output?: unknown };
    outputs.set(p.toolCallId, { ok: p.ok !== false, text: typeof p.output === "string" ? p.output : "" });
  }
  const queries = entries
    .filter((e) => e.type === "query")
    .map((e) => {
      const p = e.payload as { toolCallId: string; input?: { spl?: string; purpose?: string } };
      const out = outputs.get(p.toolCallId);
      const rows = out?.ok ? resultCountIn(out.text) : undefined;
      return {
        ref: p.toolCallId,
        query: p.input?.spl ?? "",
        ...(rows === undefined ? {} : { rows }),
        ...(p.input?.purpose ? { purpose: p.input.purpose } : {}),
        ...(out !== undefined && !out.ok ? { failed: true as const, error: out.text.split("\n")[0]!.slice(0, 300) } : {}),
      };
    });
  const patches = entries.filter((e) => e.type === "graph-patch").map((e) => e.payload as { patch?: GraphPatch; result?: { idMap?: Record<string, string> } });
  const settled = patches.find((p) => p.patch?.predictionResult)?.patch?.predictionResult;
  const theories = patches.find((p) => p.patch?.setTheories)?.patch?.setTheories?.length;
  const changed = entries
    .filter((e) => e.type === "confidence-change")
    .map((e) => {
      const c = e.payload as { id: string; from: number; to: number; status: string; prevStatus: string };
      return { id: c.id, status: c.status, confidence: c.to, from: c.from, from_status: c.prevStatus };
    });
  return {
    n: turn,
    move: decision.move,
    targetId: decision.targetId,
    reason: decision.reason ?? "",
    queries,
    added: [...new Set(patches.flatMap((p) => Object.values(p.result?.idMap ?? {})))],
    changed,
    target: "",
    created: [],
    ...(settled === undefined ? {} : { prediction: { id: settled.id, status: settled.status } }),
    ...(theories === undefined ? {} : { theories }),
    ...(patches.some((p) => p.patch?.setConclusion) ? { concluded: true as const } : {}),
  };
}

const LABEL_MAX = 120;
const clip = (s: string) => (s.length > LABEL_MAX ? `${s.slice(0, LABEL_MAX - 1)}…` : s);

// The target's label and the nodes and links this turn created, read off the graph it left.
function describe(step: V3Step, g: Graph): V3Step {
  const edge = g.edges[step.targetId];
  const prediction = (g.theories ?? []).flatMap((t) => t.predictions).find((p) => p.id === step.targetId);
  const target = g.nodes[step.targetId]?.label ?? (edge ? `${edge.source} → ${edge.target}` : prediction?.expect ?? "");
  const created: V3Step["created"] = [];
  for (const id of step.added) {
    const node = g.nodes[id];
    const link = g.edges[id];
    if (node?.createdTurn === step.n) {
      const kind = node.kind === "actor" ? "actor" : node.kind === "boundary-marker" ? "boundary" : getChildren(g, id).some((e) => e.target === step.targetId) ? "cause" : "event";
      created.push({ id, kind, label: clip(node.label) });
    } else if (link?.createdTurn === step.n) created.push({ id, kind: "link", label: "" });
  }
  return { ...step, target: clip(target), created };
}

// The run as its ledger holds it: what a resume continues from and every reader shows.
export function fold(events: readonly AgentEvent<V3Kinds>[]): Folded {
  const out: Folded = { setup: null, graph: null, entries: [], lastTurn: -1, exit: null, report: null, steps: [] };
  for (const event of events) {
    if (event.kind === "rootcause_setup") {
      out.setup = event.payload;
      out.graph = seedFromAlert(createGraph(event.run_id), event.payload.alert, event.payload.cfg);
    } else if (event.kind === "rootcause_turn" && out.setup !== null && out.graph !== null) {
      const { turn, rare, entries } = event.payload;
      if (rare !== undefined) out.graph.rare = rare;
      // In the order the turn applied them: the move's own, then a probe the model chose next.
      for (const e of entries) if (e.type === "graph-patch") applyPatch(out.graph, (e.payload as { patch: GraphPatch }).patch, turn, out.setup.cfg);
      out.entries.push(...entries);
      out.lastTurn = turn;
      const step = stepOf(turn, entries);
      if (step !== null) out.steps.push(describe(step, out.graph));
    } else if (event.kind === "rootcause_exit") {
      out.exit = event.payload.exit;
      out.report = event.payload.report;
      out.entries.push(...event.payload.entries);
    }
  }
  return out;
}
