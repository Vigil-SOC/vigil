import type { AgentEvent, CallPayload, TerminalPayload } from "../../../contracts/events.js";
import type { SpendPayload } from "../../../core/seams.js";
import type { RootCauseProjection, SearchView } from "../projection.js";
import type { RootCauseReplay, RootCauseReplayStep } from "../replay.js";
import { RECENT_SEARCHES } from "../projection.js";
import type { Proof, StepPayload } from "../proof.js";
import { bestPathFromSymptom, getParents, type Graph, type GraphEdge, type GraphNode } from "./graph.js";
import { fold, type V3Kinds, type V3Step } from "./ledger.js";
import { disposition, forReport, reportRows, type AccountRow, type ChainEvent, type ReportRows, type TimelineRow } from "./report.js";

// A v3 run in the shape the console already reads a root-cause run in: its chain as
// steps, symptom first, each tied to the event before it. Beside that, the run's own
// account, computed on each read by the functions that write report.md (report.ts reportRows)
// so the two never differ: who is who, the chain root first with ties and evidence, the
// timeline with who ran and who operated, other findings, targeted searches, open questions.

export interface V3Projection extends RootCauseProjection {
  engine: "rootcause-v3";
  exit: string | null;
  // report.md as the run wrote it, once it has.
  report: string | null;
  moves: V3Step[];
  account: V3Account | null;
  // Root first. A tie runs to the next event; an evidence ref resolves through moves[].queries[].ref.
  chain: { score: number; events: ChainEvent[]; alternatives: ReportRows["alternatives"] } | null;
  timeline: { events: TimelineRow[]; impersonation: ReportRows["impersonation"]; exonerated: ReportRows["exonerated"] };
  other_findings: ReportRows["others"];
  targeted_searches: ReportRows["tested"];
  open_questions: ReportRows["open"];
}

export interface V3Account {
  alert: { id: string; title: string; observedAt: string } | null;
  disposition: string;
  exit: string | null;
  root_cause: { id: string; label: string; justification?: string } | null;
  conclusion: { text: string; chain: string[]; openQuestions: string[] } | null;
  accounts: AccountRow[];
}

const proofOf = (x: { status: string } | undefined): Proof =>
  x === undefined ? "none" : x.status === "confirmed" ? "proven" : x.status === "ruled-out" ? "rejected" : "unproven";

function actorOf(g: Graph, node: GraphNode): string {
  const by = getParents(g, node.id, "attributed-to").filter((e) => !e.retired).sort((a, b) => b.confidence - a.confidence)[0];
  return by === undefined ? "" : (g.nodes[by.source]?.label ?? "");
}

function chainOf(g: Graph): StepPayload[] {
  const path = bestPathFromSymptom(g);
  if (path === null) return [];
  return path.nodeIds.map((id, i) => {
    const node = g.nodes[id]!;
    const tie: GraphEdge | undefined = path.edgeIds[i] === undefined ? undefined : g.edges[path.edgeIds[i]!];
    const artifact = node.artifacts[0];
    return {
      step_id: id,
      event: node.label,
      who: actorOf(g, node),
      at: node.observedAt ?? "",
      link: tie === undefined ? "" : (tie.evidence[0]?.summary ?? tie.kind),
      artifact: artifact === undefined ? "" : `${artifact.kind}:${artifact.value}`,
      cause_id: path.nodeIds[i + 1] ?? null,
      origin: node.isRoot === true,
      link_status: proofOf(tie),
      origin_status: node.isRoot === true ? proofOf(node) : "none",
    };
  });
}

// The search a call ran: the evidence tool's spl, else the arguments as they stand.
function queryOf(raw: string): string {
  try {
    const spl = (JSON.parse(raw) as { spl?: unknown }).spl;
    return typeof spl === "string" ? spl : raw;
  } catch {
    return raw;
  }
}

export function rootCauseV3Projection(runId: string, events: readonly AgentEvent<Record<never, never>>[]): V3Projection {
  const ledger = events as readonly AgentEvent<V3Kinds>[];
  const folded = fold(ledger);
  let terminal: TerminalPayload | null = null;
  let cost: number | null = null;
  let ceiling: number | null = null;
  const searches: SearchView[] = [];
  for (const event of ledger) {
    if (event.kind === "run") {
      const limit = (event.payload as { budgets?: { max_cost_usd?: unknown } }).budgets?.max_cost_usd;
      if (typeof limit === "number") ceiling = limit;
    } else if (event.kind === "spend") {
      const spent = (event.payload as SpendPayload).cost_usd;
      if (spent !== null) cost = (cost ?? 0) + spent;
    } else if (event.kind === "call") {
      const call = event.payload as CallPayload;
      searches.push({ tool: call.tool, args: queryOf(call.arguments).slice(0, 500), rows: call.ok ? 1 : 0, failed: !call.ok });
    } else if (event.kind === "terminal") {
      terminal = event.payload as TerminalPayload;
    }
  }
  const g = folded.graph === null ? null : forReport(folded.graph);
  // Null while the run is in flight. The word is the report's own rule, including when the exit
  // arrived before a graph did: root-cause-found and false-positive need none.
  const verdict = disposition(g, folded.exit);
  // The trace as the report reads it: by ts, then id (the journal holds it in the order it was written).
  const rows = g === null ? null : reportRows(g, [...folded.entries].sort((a, b) => a.ts.localeCompare(b.ts) || a.id.localeCompare(b.id)));
  const named = g === null ? undefined : g.nodes[rows?.conclusion?.rootCause?.id ?? ""] ?? Object.values(g.nodes).find((n) => n.isRoot);
  const steps = (folded.graph === null ? [] : chainOf(folded.graph)).map((step) => ({ ...step, proven: step.link_status === "proven" || (step.origin && step.origin_status === "proven") }));
  return {
    run_id: runId,
    run_kind: "root_cause",
    status: terminal !== null ? "terminal" : "running",
    outcome: terminal?.outcome ?? null,
    reason: terminal?.reason ?? "",
    cost_usd: cost,
    max_cost_usd: ceiling,
    disposition: folded.exit === null ? null : verdict.word,
    steps,
    proven: steps.filter((step) => step.proven).length,
    notices: [],
    searches: searches.length,
    recent_searches: searches.slice(-RECENT_SEARCHES),
    open_checkpoint: null,
    engine: "rootcause-v3",
    exit: folded.exit,
    report: folded.report,
    moves: folded.steps,
    account: g === null || rows === null ? null : {
      alert: folded.setup === null ? null : { id: folded.setup.alert.id, title: folded.setup.alert.title, observedAt: folded.setup.alert.observedAt },
      disposition: verdict.sentence,
      exit: folded.exit,
      root_cause: named === undefined ? null : { id: named.id, label: named.label, ...(named.rootJustification ? { justification: named.rootJustification } : {}) },
      conclusion: rows.conclusion && { text: rows.conclusion.text, chain: rows.conclusion.chain, openQuestions: rows.conclusion.openQuestions },
      accounts: rows.accounts,
    },
    chain: rows?.chain ? { ...rows.chain, alternatives: rows.alternatives } : null,
    timeline: { events: rows?.timeline ?? [], impersonation: rows?.impersonation ?? [], exonerated: rows?.exonerated ?? [] },
    other_findings: rows?.others ?? [],
    targeted_searches: rows?.tested ?? [],
    open_questions: rows?.open ?? [],
  };
}

// What the run did, in ledger order, in the shape the console replays a root-cause run in: each move as a
// notice, the searches it ran, and the chain as it stands at the end. The moves themselves ride along.
export function rootCauseV3Replay(runId: string, events: readonly AgentEvent<Record<never, never>>[]): RootCauseReplay & { engine: "rootcause-v3"; moves: V3Step[] } {
  const ledger = events as readonly AgentEvent<V3Kinds>[];
  const folded = fold(ledger);
  const byTurn = new Map(folded.steps.map((step) => [step.n, step]));
  const steps: RootCauseReplayStep[] = [];
  let budgets: RootCauseReplay["budgets"];
  let last = "";
  for (const event of ledger) {
    last = event.ts;
    if (event.kind === "run") budgets = (event.payload as { budgets?: RootCauseReplay["budgets"] }).budgets;
    if (event.kind !== "rootcause_turn") continue;
    const step = byTurn.get(event.payload.turn);
    if (step === undefined) continue;
    steps.push({ kind: "notice", recorded_at: event.ts, text: `Turn ${step.n}: ${step.move} ${step.targetId}. ${step.reason}` });
    for (const q of step.queries) steps.push({ kind: "search", recorded_at: event.ts, tool: "splunk", args: q.query.slice(0, 500), rows: q.rows ?? 0, failed: q.failed === true, ...(q.error === undefined ? {} : { failure: q.error }) });
  }
  if (folded.graph !== null) for (const link of chainOf(folded.graph)) steps.push({ kind: "step", recorded_at: last, ...link });
  return { run_id: runId, run_kind: "root_cause", ...(budgets === undefined ? {} : { budgets }), steps, engine: "rootcause-v3", moves: folded.steps };
}
