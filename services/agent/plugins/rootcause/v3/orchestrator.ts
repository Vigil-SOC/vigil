// The loop (spec §8): the model names each move, router dispatches, the handler returns a patch, the graph applies
// and rescores it, the trace records it. Handed to the caller after every turn so a run can resume.
import type { AgentConfig } from "./config.js";
import { applyPatch, bestPathFromSymptom, createGraph, getParents, emptyPatch, type Graph, type GraphPatch } from "./graph.js";
import { renderReport } from "./report.js";
import { settled } from "./scoring.js";
import { rareSweep } from "./rare.js";
import { route } from "./router.js";
import { fallback, options, optionsText, resolveNext, type LeadState, type NextIn } from "./lead.js";
import type { ModelClient } from "./model/client.js";
import type { FrontierDecision } from "./frontier.js";
import type { Alert, Deps, ExitReason, LoopContext, RunResult, Spent } from "./types.js";

// onTurn gets the graph after the rare sweep (turn 0) and after every turn: what a resume starts from.
// signal: an operator's stop or a lost lease; the run throws its reason at the next turn rather than start one.
export interface RunOpts { runId: string; range: [string, string]; index: string; graph?: Graph; startTurn?: number; signal?: AbortSignal; log?: (s: string) => void; onTurn?: (graph: Graph, turn: number) => void | Promise<void> }

export function seedFromAlert(g: Graph, alert: Alert, cfg: AgentConfig): Graph {
  applyPatch(g, { ...emptyPatch(), addNodes: [{ tempId: "symptom", kind: "action", label: alert.title, domain: alert.source, prior: 0.5, evidence: [], artifacts: alert.artifacts, observedAt: alert.observedAt, isSymptom: true }] }, 0, cfg);
  return g;
}

// The chain to the alert is done: its top is the root, every event and link on it confirmed, no open boundary,
// and each action's ran-as identity settled (top confirmed, rivals <= competitorMax).
export function chainSolid(g: Graph, cfg: AgentConfig): boolean {
  const path = bestPathFromSymptom(g);
  if (path) {
    const nodes = path.nodeIds.map((id) => g.nodes[id]!);
    const top = nodes[nodes.length - 1]!;
    const ok = (x: { status: string; confidence: number }) => settled(x, cfg);
    const solid = nodes.every((n) => n.kind !== "boundary-marker" && ok(n)) && path.edgeIds.every((id) => ok(g.edges[id]!));
    const attributed = nodes.filter((n) => n.kind === "action").every((n) => {
      const es = getParents(g, n.id, "attributed-to").filter((e) => !e.retired).sort((a, b) => b.confidence - a.confidence);
      return !es.length || (ok(es[0]!) && es.slice(1).every((e) => e.confidence <= cfg.thresholds.competitorMax));
    });
    return !!top.isRoot && solid && attributed;
  }
  return false;
}

// Stops the run early. A solid chain does not stop it: branch moves keep widening the picture until the
// frontier has nothing left or the budget runs out (then the exit is root-cause-found if the chain is solid).
export function checkExit(g: Graph, cfg: AgentConfig, spent: Spent): ExitReason | null {
  if (Object.values(g.nodes).some((n) => n.isSymptom && n.status === "ruled-out")) return "false-positive";
  if (spent.usd >= cfg.maxUsd) return "budget-exhausted";
  return null;
}

// The least kept back for the closing turn, before the run has a move's cost to go by.
const FLOOR_USD = 0.5;

export async function run(alert: Alert, cfg: AgentConfig, deps: Deps, o: RunOpts): Promise<RunResult> {
  const log = o.log ?? (() => {});
  const graph = o.graph ?? seedFromAlert(createGraph(o.runId), alert, cfg);
  // Rare activity per source, once per run (kept in the graph, so a resume reuses it), into the cached context.
  if (graph.rare === undefined && cfg.rareSweep) {
    graph.rare = await rareSweep({ tools: deps.tools, trace: deps.trace.forTurn(0, undefined, "rare"), index: o.index, range: o.range, domains: deps.domains, timeoutMs: cfg.toolTimeoutMs }).catch((e) => { log(`rare sweep failed: ${e}`); return ""; });
    log(`rare sweep: ${graph.rare.split("\n").length} lines`);
  }
  if (graph.rare) deps.model.addContext?.(graph.rare);
  o.signal?.throwIfAborted(); // a sweep cut short by a stop or a lost lease is not kept
  await o.onTurn?.(graph, 0);
  let exit: ExitReason | null = null;
  // The model names every move: each submission's `next`, checked against the graph, is the next turn's move.
  // With none (turn 1, a resume, a missing or refused `next`) a choose turn asks for it.
  const st: LeadState = {};
  let pending: FrontierDecision | undefined;
  // Every prompt says how much budget is left; under wrapUpAt the model is told to wrap up, and under concludeAt
  // (or when the run would end anyway) one closing conclude turn writes the model's own verdict.
  let cur = o.startTurn ?? 1;
  const usdLeft = () => cfg.maxUsd - deps.budget.usd;
  // Framing and scoping are not moves: a fresh run's average starts after them.
  const setupUsd = (o.startTurn ?? 1) <= 1 ? deps.budget.usd : 0;
  // What the run keeps back: one more move at the average cost so far plus the closing turn (about a move's cost).
  // The floor holds before any move has run.
  const reserve = () => (cur > 1 ? Math.max(FLOOR_USD, (2 * (deps.budget.usd - setupUsd)) / (cur - 1)) : FLOOR_USD);
  const lowOn = (share: number) => usdLeft() <= Math.max(reserve(), share * cfg.maxUsd);
  const budgetText = () => {
    const per = deps.budget.usd / Math.max(1, cur - 1);
    const wrap = lowOn(cfg.wrapUpAt);
    return `<budget>\nspent $${deps.budget.usd.toFixed(2)} of $${cfg.maxUsd} ($${usdLeft().toFixed(2)} left, about ${Math.max(0, Math.floor(usdLeft() / per))} more turns at the rate so far)${wrap ? "\nRunning out: under 10% left. Wrap up: open no new threads. Finish the thread you are on and link what you found to the chain. Do not pick a root cause to have one: if the records do not show where the attack began, it stays open. A closing turn, where you write your conclusion, follows automatically." : ""}\n</budget>`;
  };
  const model: ModelClient = {
    runTurn: async (req) => {
      if (req.move === "scope" || req.move === "conclude") return deps.model.runTurn(req);
      const choosing = req.move === "choose";
      const chose = curSource ? `\n\n<lead_note>You chose to look in ${curSource} first for this move.</lead_note>` : "";
      const r = await deps.model.runTurn({ ...req, userContent: `${req.userContent}${chose}\n\n${budgetText()}\n\n${optionsText(graph, cfg, st, cur, choosing)}` });
      // A choose answer is the choice itself; any other submission carries it under `next`.
      const nx = choosing ? (r.submission as NextIn | null) : (r.submission as { next?: NextIn } | null)?.next;
      if (nx) st.next = nx;
      // A turn with no valid submission is one attempt on its target (the handlers count it); the next prompt says so.
      st.failed = choosing ? r.failed : undefined;
      st.silent = r.submission === null && !choosing ? `${req.move} ${req.correlationId}` : undefined;
      return r;
    },
  };
  let curSource: string | undefined; // the sourcetype the model chose for this turn's move
  let concluding: ExitReason | null = null; // the exit to take once the conclude turn has run

  for (let turn = o.startTurn ?? 1; ; turn++) {
    o.signal?.throwIfAborted();
    cur = turn;
    if (concluding && graph.conclusion) { exit = concluding; break; }
    exit = checkExit(graph, cfg, deps.budget);
    if (exit === "budget-exhausted" && chainSolid(graph, cfg)) exit = "root-cause-found";
    // Nothing open on the graph ends the run; otherwise the model's last choice runs, or it is asked to choose.
    if (!exit && !pending && !Object.keys(options(graph, cfg)).length) exit = chainSolid(graph, cfg) ? "root-cause-found" : "inconclusive";
    const symptomId = Object.values(graph.nodes).find((n) => n.isSymptom)!.id;
    let decision: FrontierDecision | null = exit ? null : pending ? { ...pending, turn } : { move: "choose", targetId: symptomId, turn, reason: st.refused ?? st.failed ?? "no next move named" };
    pending = undefined;
    st.next = undefined;
    if (!exit && cfg.concludeAt > 0 && lowOn(cfg.concludeAt)) exit = chainSolid(graph, cfg) ? "root-cause-found" : "budget-exhausted";
    if (exit) {
      if (concluding || graph.conclusion || cfg.concludeAt <= 0) break;
      concluding = exit;
      const why = exit === "inconclusive" ? "Nothing is left to do on the graph." : exit === "false-positive" ? "The alerted event was refuted." : usdLeft() <= reserve() || usdLeft() <= cfg.concludeAt * cfg.maxUsd || exit === "budget-exhausted" ? "The budget is used up." : "The chain to the alert is settled.";
      decision = { move: "conclude", targetId: symptomId, turn, reason: `the run ends (${exit}): ${why}` };
    }
    if (!decision) break;
    curSource = decision.source;

    const tt = deps.trace.forTurn(turn, decision.move, decision.targetId);
    tt.write("decision", decision);
    log(`turn ${turn} · ${decision.move} ${decision.targetId} · $${deps.budget.usd.toFixed(2)} · ${decision.reason}`);
    const ctx: LoopContext = { runId: graph.runId, turn, cfg, model, tools: deps.tools, trace: tt, budget: deps.budget, domains: deps.domains, sizes: deps.sizes, range: o.range, index: o.index, signal: o.signal ?? new AbortController().signal };

    const patch: GraphPatch = await route(decision, graph, ctx);
    const result = applyPatch(graph, patch, turn, cfg);
    tt.write("graph-patch", { patch, result: { idMap: result.idMap, rejected: result.rejected, touched: result.touched, resolvedMarkers: result.resolvedMarkers } });
    for (const c of result.changes) tt.write("confidence-change", c);
    for (const r of result.rejected) log(`  rejected: ${r.reason}`);
    const created = [...new Set(Object.values(result.idMap))].filter((id) => (graph.nodes[id]?.createdTurn ?? graph.edges[id]?.createdTurn) === turn);
    for (const id of created) log(`  new ${id} ${graph.nodes[id] ? graph.nodes[id]!.label : `${graph.edges[id]!.source} ${graph.edges[id]!.kind} ${graph.edges[id]!.target}`}`);
    for (const m of result.resolvedMarkers) log(`  boundary ${m} resolved`);
    for (const c of result.changes) log(`  ${c.id} ${c.from.toFixed(2)} -> ${c.to.toFixed(2)} ${c.status}`);
    tt.relate([...new Set([...result.touched, ...Object.values(result.idMap)])]);
    const r = resolveNext(st.next, graph, cfg, turn, created, { index: o.index, range: o.range });
    if (r.probe) {
      const probe = { ...emptyPatch(), addProbe: r.probe };
      const applied = applyPatch(graph, probe, turn, cfg);
      tt.write("graph-patch", { patch: probe, result: { idMap: applied.idMap, rejected: applied.rejected, touched: applied.touched, resolvedMarkers: applied.resolvedMarkers } });
    }
    pending = r.decision;
    const refused = r.refused ?? (decision.move === "choose" && !st.next && st.failed === undefined ? "that was not a valid choice." : undefined);
    st.refused = refused;
    // A refused choose turn is not asked again: an available move runs, so a refusal costs one extra turn at most.
    const fb = decision.move === "choose" && refused ? fallback(graph, cfg, st.next, turn + 1) : undefined;
    if (fb) {
      pending = { ...fb, reason: `fallback after a refused choice: ${refused}` };
      st.refused = `${refused} ${fb.move} ${fb.targetId} runs instead.`;
      log(`  fallback: ${fb.move} ${fb.targetId}`);
    }
    if (st.next || fb) tt.write("reasoning", { ...(st.next ? { lead: st.next } : {}), ...(refused ? { refused } : {}), ...(pending ? { next: `${pending.move} ${pending.targetId}` } : {}) });
    if (r.refused) log(`  lead refused: ${r.refused}`);
    await o.onTurn?.(graph, turn);
  }

  deps.trace.forTurn(-1, undefined, "run").write("exit", { exit, budget: { usd: deps.budget.usd } });
  log(`exit: ${exit} · $${deps.budget.usd.toFixed(2)}`);
  const report = renderReport(graph, deps.trace, exit, alert, deps.budget);
  return { runId: graph.runId, exit: exit ?? "inconclusive", rootPath: bestPathFromSymptom(graph), report };
}
