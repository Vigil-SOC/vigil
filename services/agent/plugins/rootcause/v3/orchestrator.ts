// The loop (spec §8): frontier decides, router dispatches, the handler returns a patch, the graph applies
// and rescores it, the trace records it. Handed to the caller after every turn so a run can resume.
import type { AgentConfig } from "./config.js";
import type { Budget } from "./budget.js";
import { selectFrontier, toConnect } from "./frontier.js";
import { applyPatch, bestPathFromSymptom, createGraph, getParents, emptyPatch, type Graph, type GraphPatch } from "./graph.js";
import { renderReport } from "./report.js";
import { rareSweep } from "./rare.js";
import { route } from "./router.js";
import { optionsText, resolveNext, type LeadState, type NextIn } from "./lead.js";
import type { ModelClient } from "./model/client.js";
import type { FrontierDecision } from "./frontier.js";
import type { Alert, Deps, ExitReason, LoopContext, RunResult } from "./types.js";

const NETWORK = /connection error|fetch failed|EAI_AGAIN|ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|socket hang up|network|timed out|timeout|overloaded|529|503/i;

// onTurn gets the graph after the rare sweep (turn 0) and after every turn: what a resume starts from.
export interface RunOpts { runId: string; range: [string, string]; index: string; graph?: Graph; startTurn?: number; log?: (s: string) => void; onTurn?: (graph: Graph, turn: number) => void | Promise<void> }

export function seedFromAlert(g: Graph, alert: Alert, cfg: AgentConfig): Graph {
  applyPatch(g, { ...emptyPatch(), addNodes: [{ tempId: "symptom", kind: "action", label: alert.title, domain: alert.source, prior: 0.5, evidence: [], artifacts: alert.artifacts, observedAt: alert.observedAt, isSymptom: true }] }, 0, cfg);
  return g;
}

// The chain to the alert is done: its top is the root, every event and link on it confirmed, no open boundary,
// and each action's ran-as identity settled (top confirmed, rivals <= competitorMax).
export function chainSolid(g: Graph, cfg: AgentConfig): boolean {
  const path = bestPathFromSymptom(g);
  if (path) {
    const nodes = path.nodeIds.map((id) => g.nodes[id]);
    const top = nodes[nodes.length - 1];
    // Near-certain counts as solid: the evidence rule can leave a 0.99 link suspected.
    const ok = (x: { status: string; confidence: number }) => x.status === "confirmed" || x.confidence >= cfg.thresholds.settled;
    const solid = nodes.every((n) => n.kind !== "boundary-marker" && ok(n)) && path.edgeIds.every((id) => ok(g.edges[id]));
    const attributed = nodes.filter((n) => n.kind === "action").every((n) => {
      const es = getParents(g, n.id, "attributed-to").filter((e) => !e.retired).sort((a, b) => b.confidence - a.confidence);
      return !es.length || (ok(es[0]) && es.slice(1).every((e) => e.confidence <= cfg.thresholds.competitorMax));
    });
    return !!top.isRoot && solid && attributed;
  }
  return false;
}

// Stops the run early. A solid chain does not stop it: branch moves keep widening the picture until the
// frontier has nothing left or the budget runs out (then the exit is root-cause-found if the chain is solid).
export function checkExit(g: Graph, cfg: AgentConfig, turn: number, budget: Budget): ExitReason | null {
  if (Object.values(g.nodes).some((n) => n.isSymptom && n.status === "ruled-out")) return "false-positive";
  if (turn > cfg.maxTurns || budget.billedTokens > cfg.maxTotalTokens || budget.minutes > cfg.maxWallClockMinutes || budget.usd >= cfg.maxUsd) return "budget-exhausted";
  return null;
}

export async function run(alert: Alert, cfg: AgentConfig, deps: Deps, o: RunOpts): Promise<RunResult> {
  const log = o.log ?? (() => {});
  const graph = o.graph ?? seedFromAlert(createGraph(o.runId), alert, cfg);
  // Rare activity per source, once per run (kept in the graph, so a resume reuses it), into the cached context.
  if (graph.rare === undefined && cfg.rareSweep) {
    graph.rare = await rareSweep({ tools: deps.tools, trace: deps.trace.forTurn(0, undefined, "rare"), index: o.index, range: o.range, domains: deps.domains, timeoutMs: cfg.toolTimeoutMs }).catch((e) => { log(`rare sweep failed: ${e}`); return ""; });
    log(`rare sweep: ${graph.rare.split("\n").length} lines`);
  }
  if (graph.rare) deps.model.addContext?.(graph.rare);
  await o.onTurn?.(graph, 0);
  let exit: ExitReason | null = null;
  let failures = 0;
  // Model-led (cfg.lead): every move's prompt lists the moves available now and the submission names the next
  // one; the frontier picks only when the model's choice is missing or not available.
  const st: LeadState = {};
  let pending: FrontierDecision | undefined;
  // Every prompt says how much budget is left; under wrapUpAt the model is told to wrap up, and under concludeAt
  // (or when the run would end anyway) one closing conclude turn writes the model's own verdict.
  let cur = o.startTurn ?? 1;
  const usdLeft = () => cfg.maxUsd - deps.budget.usd;
  const turnsCapped = cfg.maxTurns < 1e8;
  const lowOn = (share: number, min = 0) => usdLeft() <= Math.max(min, share * cfg.maxUsd) || (turnsCapped && cfg.maxTurns - cur + 1 <= Math.max(1, Math.round(share * cfg.maxTurns)));
  const budgetText = () => {
    const per = deps.budget.usd / Math.max(1, cur - 1);
    const wrap = lowOn(cfg.wrapUpAt);
    return `<budget>\nspent $${deps.budget.usd.toFixed(2)} of $${cfg.maxUsd} ($${usdLeft().toFixed(2)} left, about ${Math.max(0, Math.floor(usdLeft() / per))} more turns at the rate so far)${turnsCapped ? `; turn ${cur} of ${cfg.maxTurns}` : ""}${wrap ? "\nRunning out: under 10% left. Wrap up: open no new threads. Finish the thread you are on and link what you found to the chain. Do not pick a root cause to have one: if the records do not show where the attack began, it stays open. A closing turn, where you write your conclusion, follows automatically." : ""}\n</budget>`;
  };
  const model: ModelClient = {
    runTurn: async (req) => {
      if (req.move === "scope" || req.move === "conclude") return deps.model.runTurn(req);
      const chose = cfg.lead && curSource ? `\n\n<lead_note>You chose to look in ${curSource} first for this move.</lead_note>` : "";
      const r = await deps.model.runTurn({ ...req, userContent: `${req.userContent}${chose}\n\n${budgetText()}${cfg.lead ? `\n\n${optionsText(graph, cfg, st, cur)}` : ""}` });
      const nx = (r.submission as { next?: NextIn } | null)?.next;
      if (nx) st.next = nx;
      return r;
    },
  };
  let curSource: string | undefined; // the sourcetype the model chose for this turn's move
  let concluding: ExitReason | null = null; // the exit to take once the conclude turn has run

  for (let turn = o.startTurn ?? 1; ; turn++) {
    cur = turn;
    if (concluding && graph.conclusion) { exit = concluding; break; }
    exit = checkExit(graph, cfg, turn, deps.budget);
    if (exit === "budget-exhausted" && chainSolid(graph, cfg)) exit = "root-cause-found";
    let decision = exit ? null : pending ? { ...pending, turn } : selectFrontier(graph, cfg, turn, deps.tools.coverage);
    pending = undefined;
    st.next = undefined;
    if (!exit && !decision) exit = chainSolid(graph, cfg) ? "root-cause-found" : "inconclusive";
    if (!exit && cfg.concludeAt > 0 && lowOn(cfg.concludeAt, 0.5)) exit = chainSolid(graph, cfg) ? "root-cause-found" : "budget-exhausted";
    if (exit) {
      if (concluding || graph.conclusion || cfg.concludeAt <= 0) break;
      concluding = exit;
      const symptom = Object.values(graph.nodes).find((n) => n.isSymptom)!;
      const why = exit === "inconclusive" ? "Nothing is left to do on the graph." : exit === "false-positive" ? "The alerted event was refuted." : usdLeft() <= 0.5 || usdLeft() <= cfg.concludeAt * cfg.maxUsd || exit === "budget-exhausted" ? "The budget is used up." : "The chain to the alert is settled.";
      decision = { move: "conclude", targetId: symptom.id, turn, priority: -2, reason: `the run ends (${exit}): ${why}` };
    }
    if (!decision) break;
    curSource = decision.source;

    const tt = deps.trace.forTurn(turn, decision.move, decision.targetId);
    tt.write("decision", decision);
    log(`turn ${turn} · P${decision.priority} ${decision.move} ${decision.targetId} · $${deps.budget.usd.toFixed(2)} · ${decision.reason}`);
    const ctx: LoopContext = { runId: graph.runId, turn, cfg, model, tools: deps.tools, trace: tt, budget: deps.budget, domains: deps.domains, sizes: deps.sizes, range: o.range, index: o.index, signal: new AbortController().signal };

    let patch: GraphPatch;
    try {
      // A network outage is not the move's fault: wait and retry the same move, up to about 10 minutes.
      for (let wait = 1; ; wait++) {
        try { patch = await route(decision, graph, ctx); break; } catch (e: any) {
          if (!NETWORK.test(String(e?.message ?? e) + String(e?.cause?.message ?? e?.cause?.cause?.message ?? "")) || wait > 20) throw e;
          tt.write("rejected-submission", { networkRetry: wait, error: String(e?.message ?? e) });
          log(`  network error, retrying in 30s (${wait}/20): ${e?.message ?? e}`);
          await new Promise((r) => setTimeout(r, 30_000));
        }
      }
      failures = 0;
    } catch (e: any) {
      // A failed turn counts as an attempt on its target, so the frontier moves on; three in a row stop the run.
      tt.write("rejected-submission", { error: String(e?.message ?? e) });
      log(`  turn failed: ${e?.message ?? e}`);
      if (++failures >= 3) throw e;
      patch = { ...emptyPatch(), incrementAttempts: [decision.targetId], ...(decision.move === "expand" ? { incrementExpand: [decision.targetId] } : {}), ...(decision.move === "fork" ? { incrementForks: [decision.targetId] } : {}), ...(decision.move === "branch" ? { incrementBranch: [decision.targetId] } : {}), ...(decision.move === "connect" ? { incrementConnect: toConnect(graph).map((n) => n.id) } : {}) };
    }
    const result = applyPatch(graph, patch, turn, cfg);
    tt.write("graph-patch", { patch, result: { idMap: result.idMap, rejected: result.rejected, touched: result.touched, resolvedMarkers: result.resolvedMarkers } });
    for (const c of result.changes) tt.write("confidence-change", c);
    for (const r of result.rejected) log(`  rejected: ${r.reason}`);
    const created = [...new Set(Object.values(result.idMap))].filter((id) => (graph.nodes[id]?.createdTurn ?? graph.edges[id]?.createdTurn) === turn);
    for (const id of created) log(`  new ${id} ${graph.nodes[id] ? graph.nodes[id].label : `${graph.edges[id].source} ${graph.edges[id].kind} ${graph.edges[id].target}`}`);
    for (const m of result.resolvedMarkers) log(`  boundary ${m} resolved`);
    for (const c of result.changes) log(`  ${c.id} ${c.from.toFixed(2)} -> ${c.to.toFixed(2)} ${c.status}`);
    tt.relate([...new Set([...result.touched, ...Object.values(result.idMap)])]);
    if (cfg.lead) {
      const r = resolveNext(st.next, graph, cfg, turn, created, { index: o.index, range: o.range });
      pending = r.decision;
      st.refused = r.refused;
      if (st.next) tt.write("reasoning", { lead: st.next, ...(r.refused ? { refused: r.refused } : { next: `${r.decision!.move} ${r.decision!.targetId}` }) });
      if (r.refused) log(`  lead refused: ${r.refused}`);
    }
    await o.onTurn?.(graph, turn);
  }

  deps.trace.forTurn(-1, undefined, "run").write("exit", { exit, budget: deps.budget.toJSON() });
  log(`exit: ${exit} · $${deps.budget.usd.toFixed(2)}`);
  const report = renderReport(graph, deps.trace, exit, alert, deps.budget);
  return { runId: graph.runId, exit: exit ?? "inconclusive", rootPath: bestPathFromSymptom(graph), report };
}
