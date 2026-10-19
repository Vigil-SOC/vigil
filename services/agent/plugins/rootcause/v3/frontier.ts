// Frontier (spec §6), pure: which move to make next, against which target. Priority buckets are tried in
// order on the best path, then on alternative paths; the first bucket with a candidate wins.
import type { AgentConfig } from "./config.js";
import { allPaths, getParents, islands, type Graph, type GraphEdge, type GraphNode, type Path } from "./graph.js";

export type Move = "expand" | "strengthen" | "fork" | "pivot" | "branch" | "theorize" | "test" | "connect" | "conclude";

export interface FrontierDecision {
  move: Move;
  targetId: string;
  turn: number;
  priority: number;
  reason: string;
  source?: string; // model-led: the sourcetype the model chose to look in (a pivot searches it instead of the boundary's)
}

type Scored = { x: GraphNode | GraphEdge; rank?: number };

// Settled: nothing more to gain from checking it again. Near-certain (>= settled) even when the evidence rule
// keeps it suspected, out of attempts, or two checks did not move it.
export function isStalled(x: GraphNode | GraphEdge, cfg: AgentConfig): boolean {
  if (x.confidence >= cfg.thresholds.settled || x.strengthenAttempts >= cfg.maxStrengthenAttempts) return true;
  // Two checks that together moved it less than stallDelta (one unmoved check once left a chain at 0.83).
  const n = x.attemptConf.length;
  return n >= 2 && Math.abs(x.confidence - x.attemptConf[n - 2]!) < cfg.stallDelta;
}

export const pivotExhausted = (m: GraphNode, cfg: AgentConfig) => (m.pivotSteps ?? 0) >= cfg.maxForwardSteps;
const openMarkersOn = (g: Graph, nodeId: string) => Object.values(g.nodes).filter((n) => n.kind === "boundary-marker" && n.originNodeId === nodeId && !n.resolvedTo);
// Unlinked findings a connect move has not tried yet (at most this many per move).
export const MAX_CONNECT = 6;
export const toConnect = (g: Graph) => islands(g).filter((n) => (n.connectAttempts ?? 0) < 1).slice(0, MAX_CONNECT);
const idNum = (id: string) => [id.replace(/\d+$/, ""), Number(id.match(/\d+$/)?.[0] ?? 0)] as const;

// Lower rank first (disputed edges in P2), then lower confidence, older updatedTurn, lower id.
function pick(cands: Scored[]): GraphNode | GraphEdge {
  return [...cands].sort((a, b) => {
    if ((a.rank ?? 0) !== (b.rank ?? 0)) return (a.rank ?? 0) - (b.rank ?? 0);
    if (a.x.confidence !== b.x.confidence) return a.x.confidence - b.x.confidence;
    if (a.x.updatedTurn !== b.x.updatedTurn) return a.x.updatedTurn - b.x.updatedTurn;
    const [pa, na] = idNum(a.x.id), [pb, nb] = idNum(b.x.id);
    return pa.localeCompare(pb) || na - nb;
  })[0]!.x;
}

// Stories (theorize) and their tests come before confirming events one by one: the searches go where the
// stories disagree. T1: the first round once the symptom is settled. T2: the chosen tests of the live round.
// T3: a new round once the tests are done and a few other moves have run (or the plan is out of moves).
const openTests = (g: Graph) => (g.theories ?? []).filter((t) => t.status === "live").flatMap((t) => t.predictions).filter((p) => p.next && p.status === "open");
function stories(g: Graph, cfg: AgentConfig, turn: number, force: boolean): FrontierDecision | null {
  const symptom = Object.values(g.nodes).find((n) => n.isSymptom);
  if (!symptom || cfg.maxTheorize < 1) return null;
  const rounds = g.theorizeCount ?? 0;
  if (!rounds) return { move: "theorize", targetId: symptom.id, turn, priority: 1, reason: "no stories yet: write competing explanations of the whole incident and what each predicts" };
  const [open] = openTests(g);
  if (open) return { move: "test", targetId: open.id, turn, priority: 1, reason: `test ${open.id} in ${open.sourcetype}: ${open.expect.slice(0, 120)}` };
  if (rounds < cfg.maxTheorize && (force || turn - (g.theorizedTurn ?? 0) > cfg.theorizeEvery))
    return { move: "theorize", targetId: symptom.id, turn, priority: 1, reason: `round ${rounds + 1}: revise the stories with what the tests and the last moves found` };
  return null;
}

// A solid chain top with no live cause and no open boundary.
function expandable(g: Graph, top: GraphNode, cfg: AgentConfig): boolean {
  const hasLiveParent = getParents(g, top.id).some((e) => !e.retired && e.status !== "ruled-out" && g.nodes[e.source]?.status !== "ruled-out");
  return top.kind === "action" && !top.isRoot && !hasLiveParent && !openMarkersOn(g, top.id).length
    && (top.status === "confirmed" || top.confidence >= cfg.thresholds.expandMin);
}

function onPath(g: Graph, path: Path, cfg: AgentConfig, turn: number, best: boolean): FrontierDecision | null {
  const nodes = path.nodeIds.map((id) => g.nodes[id]!);
  const edges = path.edgeIds.map((id) => g.edges[id]!);
  const P = (p: number) => (best ? p : 7);
  const where = best ? "on the best path" : `on an alternative path (score ${path.score.toFixed(2)})`;
  const decide = (move: Move, x: GraphNode | GraphEdge, priority: number, reason: string): FrontierDecision => ({ move, targetId: x.id, turn, priority, reason });

  // P0: the symptom itself is not confirmed yet.
  const symptom = nodes[0]!;
  if (best && symptom.status === "suspected" && !isStalled(symptom, cfg))
    return decide("strengthen", symptom, 0, `symptom ${symptom.id} is not confirmed yet (${symptom.confidence.toFixed(2)})`);

  // The chain top: the last event on the path (a boundary marker stands for its origin's unknown cause).
  const top = [...nodes].reverse().find((n) => n.kind === "action")!;
  // P1a: findings the tests turned up that are not linked to the chain: link them before searching further
  // (findings a test turned up once stayed unlinked, the chain stopped at an open boundary, and the report
  // never tied them to the alert).
  const unlinked = best && (g.theorizeCount ?? 0) > 0 ? toConnect(g) : [];
  if (unlinked.length) return decide("connect", top, 1, `${unlinked.length} finding(s) not linked to the chain (${unlinked.map((n) => n.id).join(", ")}): find how they lead to ${top.id}`);
  // P1b: the chain top has not been looked behind once: its cause search goes before the stories' tests (runs
  // have spent every turn on stories and tests and never expanded the top).
  // Only once the stories are running: without them P2-P6 already reach the top in order.
  if (best && (g.theorizeCount ?? 0) > 0 && expandable(g, top, cfg) && top.expandAttempts < 1)
    return decide("expand", top, 1, `${top.id} "${top.label}" is ${top.status} (${top.confidence.toFixed(2)}) and its cause was never searched`);
  // P1c: every branchEvery turns, one branch from a solid chain event, nearest the alert first. The tests never
  // run out (each round writes new ones), so branch, which waited for the end, never ran: a run never asked
  // what the implant did next and missed what it went on to do, which earlier runs had found.
  const since = g.branchedTurn ?? g.storiesSince;
  if (best && cfg.branchEvery > 0 && since !== undefined && turn - since >= cfg.branchEvery) {
    const b = nodes.find((n) => n.kind === "action" && !n.isSymptom && !n.legitimate && !n.deadEnd && (n.branchAttempts ?? 0) < 1 && (n.status === "confirmed" || n.confidence >= cfg.thresholds.expandMin));
    if (b) return decide("branch", b, 1, `${turn - since} turns since the last branch: find what else ${b.id} "${b.label}" led to`);
  }
  // The stories' tests go before pivots: a pivot walks one source step by step (five turns once went
  // through a developer's routine CI), a test asks the question the stories disagree on.
  if (best) { const s = stories(g, cfg, turn, false); if (s) return s; }

  // P1: an open boundary on the path. Not from a legitimate step: its cause is routine work, not the attack.
  const markers = nodes.flatMap((n) => openMarkersOn(g, n.id)).filter((m) => !pivotExhausted(m, cfg) && !g.nodes[m.originNodeId!]?.legitimate);
  if (markers.length) {
    const m = pick(markers.map((x) => ({ x }))) as GraphNode;
    return decide("pivot", m, P(1), `open boundary ${m.id} on ${m.originNodeId} ${where}: investigate forward in ${m.targetDomain}`);
  }

  // P2: who did a path action is disputed. Unconfirmed but undisputed attribution waits until P4.
  // Undisputed attribution gets one check; disputed gets its own budget below (P2).
  const attrAll = nodes.filter((n) => n.kind === "action").flatMap((n) => getParents(g, n.id, "attributed-to"))
    .filter((e) => !e.retired && e.status === "suspected" && !isStalled(e, cfg) && (e.disputed || e.strengthenAttempts < 1));
  // At most 2 disputed-attribution checks per action: a dispute must not eat the run.
  const checksOn = (target: string) => getParents(g, target, "attributed-to").reduce((s, e) => s + e.strengthenAttempts, 0);
  const disputed = attrAll.filter((e) => e.disputed && checksOn(e.target) < 2);
  if (disputed.length) {
    const e = pick(disputed.map((x) => ({ x }))) as GraphEdge;
    return decide("strengthen", e, P(2), `attribution ${e.id} (${e.source} -> ${e.target}) is disputed at ${e.confidence.toFixed(2)} ${where}`);
  }

  // P3: a suspected link on the path.
  // A boundary's placeholder link has no event behind it to check.
  const fromMarker = (e: GraphEdge) => g.nodes[e.source]?.kind === "boundary-marker";
  const weakEdges = edges.filter((e) => e.status === "suspected" && !isStalled(e, cfg) && !fromMarker(e));
  if (weakEdges.length) {
    const e = pick(weakEdges.map((x) => ({ x }))) as GraphEdge;
    return decide("strengthen", e, P(3), `link ${e.id} (${e.source} explains ${e.target}) is suspected at ${e.confidence.toFixed(2)} ${where}`);
  }

  // P4: a suspected event on the path.
  const weakNodes = nodes.filter((n) => n.kind !== "boundary-marker" && n.status === "suspected" && !isStalled(n, cfg));
  if (weakNodes.length || attrAll.length) {
    const x = pick([...weakNodes, ...attrAll].map((x) => ({ x })));
    return "kind" in x && x.kind === "attributed-to"
      ? decide("strengthen", x, P(4), `attribution ${x.id} (${x.source} -> ${x.target}) is unconfirmed at ${x.confidence.toFixed(2)} ${where}`)
      : decide("strengthen", x, P(4), `${x.id} "${(x as GraphNode).label}" is suspected at ${x.confidence.toFixed(2)} ${where}`);
  }

  // P5: a weak link or event stalled: propose a competing cause for the node it explains. A stalled but
  // strong one (>= expandMin) stays as it is; looking for rivals to it wastes turns.
  const weak = (x: GraphNode | GraphEdge) => x.status === "suspected" && isStalled(x, cfg) && x.confidence < cfg.thresholds.expandMin;
  const forks: { x: GraphNode | GraphEdge; child: GraphNode }[] = [];
  edges.forEach((e, i) => { if (weak(e)) forks.push({ x: e, child: nodes[i]! }); });
  nodes.forEach((n, i) => { if (i > 0 && n.kind !== "boundary-marker" && weak(n)) forks.push({ x: n, child: nodes[i - 1]! }); });
  const forkable = forks.filter((f) => f.child.forkCount < cfg.maxForksPerNode);
  if (forkable.length) {
    const stalled = pick(forkable.map((f) => ({ x: f.x })));
    const child = forkable.find((f) => f.x === stalled)!.child;
    return decide("fork", child, P(5), `${stalled.id} stalled at ${stalled.confidence.toFixed(2)}: propose a competing cause for ${child.id} ${where}`);
  }

  // P6: the top of the path is solid and has no cause yet.
  if (expandable(g, top, cfg) && top.expandAttempts < cfg.maxExpandAttempts)
    return decide("expand", top, P(6), `${top.id} "${top.label}" is ${top.status} (${top.confidence.toFixed(2)}) with no known cause ${where}`);

  return null;
}

// P8: final check. A link or event on the chain that stalled by small movement (not by attempt count) gets
// its remaining attempts before anything off the chain: the chain decides the exit.
// P9: branch. The chain to the alert is settled as far as it goes; the incident is wider. From each solid
// event, once, ask what else it produced (other processes, logons, hosts, data moved). Root first, then down
// the path, then branch events in time order, so the attacker's later steps are reached. Events from the
// sourcetypes searched least go first: branching event after event in the source the chain already lives in
// re-reads ground the run has covered while leads in other sources wait.
// P10: one check for each off-chain event that is plausible (>= 0.5); weaker ones are left as they are.
function branching(g: Graph, cfg: AgentConfig, turn: number, path: string[], edgeIds: string[], coverage: Record<string, number>): FrontierDecision | null {
  const cov = (n: GraphNode) => coverage[n.domain] ?? 0;
  // A new round of stories only after theorizeEvery other moves, so branching runs between rounds (forcing a
  // round here once kept branch from ever running). Forced only when nothing else is left (end).
  const s = stories(g, cfg, turn, false);
  if (s) return s;
  const unsettled = [...edgeIds.map((id) => g.edges[id]!).filter((e) => g.nodes[e.source]?.kind !== "boundary-marker"), ...path.map((id) => g.nodes[id]!).filter((n) => n.kind === "action")]
    .filter((x) => x.status === "suspected" && !isStalled(x, cfg));
  if (unsettled.length) {
    const x = pick(unsettled.map((y) => ({ x: y })));
    return { move: "strengthen", targetId: x.id, turn, priority: 8, reason: `final check: ${x.id} on the chain is still suspected at ${x.confidence.toFixed(2)}` };
  }
  const solid = (n: GraphNode) => n.kind === "action" && !n.isSymptom && (n.status === "confirmed" || n.confidence >= cfg.thresholds.expandMin);
  const order = (n: GraphNode) => { const i = path.indexOf(n.id); return i >= 0 ? -1000 + i : 0; }; // path is root first
  const t = (n: GraphNode) => (n.observedAt ? Date.parse(n.observedAt) : Infinity);
  const ready = Object.values(g.nodes).filter((n) => solid(n) && !n.deadEnd && (n.branchAttempts ?? 0) < 1)
    .sort((a, b) => cov(a) - cov(b) || order(a) - order(b) || t(a) - t(b) || a.createdTurn - b.createdTurn);
  const [r] = ready;
  if (r) return { move: "branch", targetId: r.id, turn, priority: 9, reason: `${r.id} "${r.label}" is solid (${r.domain}, ${cov(r)} searches so far); find what else it led to` };
  const offChain = Object.values(g.nodes).filter((n) => n.kind === "action" && !path.includes(n.id) && n.status === "suspected" && n.strengthenAttempts < 1 && n.confidence >= 0.5);
  if (offChain.length) {
    const n = [...offChain].sort((a, b) => b.confidence - a.confidence || a.createdTurn - b.createdTurn)[0]!;
    return { move: "strengthen", targetId: n.id, turn, priority: 10, reason: `off-chain finding ${n.id} "${n.label}" is suspected at ${n.confidence.toFixed(2)}: one check` };
  }
  // Out of planned moves with budget left: keep investigating instead of stopping.
  // P11: the top of the chain is not a root yet: one more, wide, look for its cause (whole range, sweep leads).
  const top = g.nodes[path[0]!];
  const parentless = top && !Object.values(g.edges).some((e) => e.kind === "explains" && e.target === top.id && !e.retired && e.status !== "ruled-out");
  if (top?.kind === "action" && !top.isRoot && parentless && top.expandAttempts < cfg.maxExpandAttempts + 1 && !Object.values(g.nodes).some((m) => m.kind === "boundary-marker" && m.originNodeId === top.id && !m.resolvedTo && !pivotExhausted(m, cfg)))
    return { move: "expand", targetId: top.id, turn, priority: 11, reason: `out of planned moves: wide look for the cause of the chain top ${top.id} "${top.label}"` };
  // P12: branch from plausible (>= 0.4) events that were not solid enough before.
  const maybe = Object.values(g.nodes).filter((n) => n.kind === "action" && !n.isSymptom && !n.deadEnd && (n.branchAttempts ?? 0) < 1 && n.status !== "ruled-out" && n.confidence >= 0.4)
    .sort((a, b) => cov(a) - cov(b) || b.confidence - a.confidence || a.createdTurn - b.createdTurn);
  const [m] = maybe;
  if (m) return { move: "branch", targetId: m.id, turn, priority: 12, reason: `out of planned moves: branch from ${m.id} "${m.label}" (${m.confidence.toFixed(2)})` };
  return stories(g, cfg, turn, true);
}

export function selectFrontier(g: Graph, cfg: AgentConfig, turn: number, coverage: Record<string, number> = {}): FrontierDecision | null {
  const paths = allPaths(g);
  const [first] = paths;
  if (!first) return null;
  const best = onPath(g, first, cfg, turn, true);
  if (best) return best;
  for (const p of paths.slice(1)) {
    if (p.score < cfg.thresholds.altPathMin) break;
    const d = onPath(g, p, cfg, turn, false);
    if (d) return d;
  }
  return branching(g, cfg, turn, [...first.nodeIds].reverse(), first.edgeIds, coverage);
}
