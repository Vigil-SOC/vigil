// The hypothesis graph (spec §3.2–3.4). Handlers read it and return a GraphPatch; only the orchestrator
// applies one. applyPatch validates, merges, rescores and reports what changed. No I/O except load/save.
import type { AgentConfig } from "./config.js";
import { confidence, status as deriveStatus, clampPrior, type Status } from "./scoring.js";
import { STRONG_KINDS, type Artifact, type Domain, type Evidence } from "./types.js";
import { sameEvent } from "./stitch.js";

export type NodeId = string;
export type EdgeId = string;
export type NodeKind = "action" | "actor" | "boundary-marker";
// attributed-to: the account the action ran as. operated-by: who actually controlled it, when that differs
// (malware in a victim's session). explains: cause -> effect.
export type EdgeKind = "explains" | "attributed-to" | "operated-by";

export interface GraphNode {
  id: NodeId;
  kind: NodeKind;
  label: string;
  domain: Domain;
  status: Status;
  confidence: number;
  prior: number;
  evidence: Evidence[];
  artifacts: Artifact[];
  observedAt?: string;
  isSymptom?: boolean;
  isRoot?: boolean;
  rootJustification?: string;
  deadEnd?: boolean; // nothing follows from it: no branch move
  takeover?: boolean; // its label claims an account takeover, backed by a cited takeover record
  legitimate?: boolean; // routine work by its account that the attack used or passed through: not the attacker
  unverified?: boolean; // loose mode: kept although a check failed (no baseline, accusing label); never accuses in the report
  leads?: string; // what a cross-source sweep of its identifying values found (search ids included)
  swept?: boolean;
  // boundary-marker only
  targetDomain?: Domain;
  originNodeId?: NodeId;
  reason?: string;
  resolvedTo?: NodeId;
  forwardLeaves?: NodeId[];
  pivotSteps?: number;
  // frontier bookkeeping
  strengthenAttempts: number;
  attemptConf: number[]; // confidence when each strengthen attempt started
  expandAttempts: number;
  forkCount: number;
  branchAttempts: number;
  connectAttempts?: number; // connect moves that listed it as an unlinked finding
  confidenceHistory: { turn: number; value: number }[];
  createdTurn: number;
  updatedTurn: number;
}

export interface GraphEdge {
  id: EdgeId;
  kind: EdgeKind;
  source: NodeId; // cause (explains) or actor (attributed-to)
  target: NodeId; // effect (explains) or action (attributed-to)
  status: Status;
  confidence: number;
  prior: number;
  disputed: boolean;
  joint?: boolean; // needed together with the other causes of its target, not instead of them
  inferred?: boolean; // loose mode: kept without the record of a join (no shared value); its evidence counts as circumstantial
  retired?: boolean; // a resolved marker's edge: kept for audit, never on a path
  stitchFor?: NodeId; // a tentative stitch edge: confirming it resolves this marker
  evidence: Evidence[];
  strengthenAttempts: number;
  attemptConf: number[];
  confidenceHistory: { turn: number; value: number }[];
  createdTurn: number;
  updatedTurn: number;
}

export interface Graph {
  runId: string;
  nodes: Record<NodeId, GraphNode>;
  edges: Record<EdgeId, GraphEdge>;
  seq: Record<string, number>;
  theories?: Theory[]; // every round's stories; the live ones are the latest round's
  theorizeCount?: number;
  rare?: string; // the rare-activity sweep (rare.ts), computed once per run and kept for resume
  conclusion?: Conclusion; // the model's closing verdict (conclude move), written when the run ends
  theorizedTurn?: number;
  storiesSince?: number; // the turn of the first round of stories
  branchedTurn?: number; // the turn of the last branch move
}

// Competing stories of the whole incident (theorize move) and the predictions that tell them apart (test move).
export interface Prediction {
  id: string; // R<round>.P<n>
  sourcetype: string;
  expect: string;
  spl: string;
  earliest?: string;
  latest?: string;
  separates: string[]; // theory ids
  next: boolean; // chosen to test first
  status: "open" | "found" | "absent" | "unclear";
  result?: string;
  toolCallIds?: string[];
  testedTurn?: number;
}
export interface Conclusion { text: string; rootCause?: string; chain: string[]; openQuestions: string[]; turn: number }
export interface Theory {
  id: string; // R<round>.H<n>
  round: number;
  story: string;
  accountsFor: string[];
  gaps: string;
  predictions: Prediction[];
  status: "live" | "replaced";
  support: number;
  against: number;
}

export type EvidenceDraft = Omit<Evidence, "id">;

export interface NodeIn {
  tempId: string;
  kind: NodeKind;
  label: string;
  domain: Domain;
  prior: number;
  evidence: EvidenceDraft[];
  artifacts: Artifact[];
  observedAt?: string;
  isSymptom?: boolean;
  isRoot?: boolean;
  rootJustification?: string;
  deadEnd?: boolean;
  unverified?: boolean;
  takeover?: boolean;
  legitimate?: boolean;
  targetDomain?: Domain;
  originNodeId?: NodeId;
  reason?: string;
  forwardLeaves?: string[];
  pivotSteps?: number;
}

export interface EdgeIn {
  tempId: string;
  kind: EdgeKind;
  source: string; // real id or tempId
  target: string;
  prior: number;
  evidence: EvidenceDraft[];
  stitchFor?: NodeId;
  joint?: boolean;
  inferred?: boolean;
}

export interface GraphPatch {
  addNodes: NodeIn[];
  addEdges: EdgeIn[];
  addEvidence: Array<{ targetId: string; evidence: EvidenceDraft }>;
  resolveBoundary?: { markerId: NodeId; nodeId: string; joinEvidence: EvidenceDraft[] };
  setFlags?: Array<{ nodeId: string; isRoot?: boolean; rootJustification?: string; legitimate?: boolean }>;
  updateMarkers?: Array<{ markerId: NodeId; forwardLeaves?: string[]; pivotSteps?: number }>;
  addArtifacts?: Array<{ nodeId: string; artifacts: Artifact[] }>;
  setLeads?: Array<{ nodeId: NodeId; leads: string }>;
  incrementAttempts?: string[]; // strengthen attempts on a node or edge
  incrementExpand?: NodeId[];
  incrementForks?: NodeId[];
  incrementBranch?: NodeId[];
  incrementConnect?: NodeId[];
  setConclusion?: Conclusion;
  setTheories?: Theory[]; // a new round: replaces the live stories (empty: the round failed, the count still moves)
  predictionResult?: { id: string; status: Prediction["status"]; result: string; toolCallIds: string[]; supports: string[]; weakens: string[] };
  addProbe?: Prediction; // a rare record the model chose to read (lead.ts), filed under the PROBES pseudo-story
}

export const emptyPatch = (): GraphPatch => ({ addNodes: [], addEdges: [], addEvidence: [] });

export interface Change { id: string; from: number; to: number; status: Status; prevStatus: Status }

export interface ApplyResult {
  idMap: Record<string, string>;
  rejected: Array<{ item: unknown; reason: string }>;
  touched: string[];
  changes: Change[];
  resolvedMarkers: NodeId[];
}

export interface Path { nodeIds: NodeId[]; edgeIds: EdgeId[]; score: number } // symptom first

const MAX_DEPTH = 25;

export function createGraph(runId: string): Graph {
  return { runId, nodes: {}, edges: {}, seq: {} };
}

const nextId = (g: Graph, prefix: string) => `${prefix}${(g.seq[prefix] = (g.seq[prefix] ?? 0) + 1)}`;
const PREFIX: Record<NodeKind, string> = { action: "N", actor: "A", "boundary-marker": "B" };

export const isEdgeId = (g: Graph, id: string) => id in g.edges;

export function getParents(g: Graph, nodeId: NodeId, kind: EdgeKind = "explains"): GraphEdge[] {
  return Object.values(g.edges).filter((e) => e.target === nodeId && e.kind === kind);
}
export function getChildren(g: Graph, nodeId: NodeId, kind: EdgeKind = "explains"): GraphEdge[] {
  return Object.values(g.edges).filter((e) => e.source === nodeId && e.kind === kind);
}

const norm = (s: string) => s.trim().toLowerCase().replace(/\s+/g, " ");
const sameArtifact = (a: Artifact, b: Artifact) => a.kind === b.kind && norm(a.value) === norm(b.value);
const shares = (a: Artifact[], b: Artifact[], pred: (x: Artifact) => boolean) => a.some((x) => pred(x) && b.some((y) => sameArtifact(x, y)));

// Two descriptions of the same thing: an actor by its principal, an action by an event id or process guid,
// or by a strong artifact in the same domain at the same time. An equal label is the same too.
function sameThing(n: Pick<NodeIn, "kind" | "label" | "domain" | "artifacts" | "observedAt">, m: GraphNode): boolean {
  if (n.kind !== m.kind || n.kind === "boundary-marker") return false;
  // Both carry the log's own event id and none is shared: two records, whatever else they share.
  const ids = (a: Artifact[]) => a.filter((x) => x.kind === "event-id");
  if (n.kind === "action" && ids(n.artifacts).length && ids(m.artifacts).length && !shares(n.artifacts, m.artifacts, (x) => x.kind === "event-id")) return false;
  if (norm(n.label) === norm(m.label) && n.domain === m.domain) return true;
  if (n.kind === "actor") return shares(n.artifacts, m.artifacts, (x) => x.kind === "principal");
  if (shares(n.artifacts, m.artifacts, (x) => x.kind === "event-id")) return true;
  if (n.domain !== m.domain) return false;
  // The same event re-described: within 2 minutes (or a time missing), and pinned by a shared strong value (an
  // attachment hash, a digest), the same connection (both IPs) or the same full command line on the same host.
  if (sameEvent(n, m)) return true;
  const near = !n.observedAt || !m.observedAt || Math.abs(Date.parse(n.observedAt) - Date.parse(m.observedAt)) <= 120_000;
  if (!near) return false;
  // Process ids are left out: models often record the parent's id as the event's own, and parents repeat.
  if (shares(n.artifacts, m.artifacts, (x) => STRONG_KINDS.has(x.kind) && x.kind !== "process-guid")) return true;
  // ...unless the same process id comes with the same host, the same second (±2 s) and one more shared value (the
  // parent, the container, a domain or file): the same process re-described (one container start once became
  // three events, each searched for its cause).
  const close = !!n.observedAt && !!m.observedAt && Math.abs(Date.parse(n.observedAt) - Date.parse(m.observedAt)) <= 2000;
  if (close && shares(n.artifacts, m.artifacts, (x) => x.kind === "process-guid") && shares(n.artifacts, m.artifacts, (x) => x.kind === "host")
    && shares(n.artifacts, m.artifacts, (x) => ["parent-process-guid", "k8s-resource", "domain-name", "url", "file-hash", "image-digest"].includes(x.kind))) return true;
  const sharedIps = n.artifacts.filter((x) => x.kind === "ip" && m.artifacts.some((y) => sameArtifact(x, y))).length;
  if (sharedIps >= 2 && !!n.observedAt && !!m.observedAt) return true;
  return shares(n.artifacts, m.artifacts, (x) => x.kind === "process" && x.value.trim().length >= 15) && shares(n.artifacts, m.artifacts, (x) => x.kind === "host");
}

export function findByArtifacts(g: Graph, artifacts: Artifact[]): GraphNode[] {
  return Object.values(g.nodes).filter((n) => shares(artifacts, n.artifacts, () => true));
}

// Adding source -> target (explains) closes a cycle when source is already downstream of target.
function makesCycle(g: Graph, source: NodeId, target: NodeId): boolean {
  if (source === target) return true;
  const stack = [target];
  const seen = new Set<string>();
  while (stack.length) {
    const n = stack.pop()!;
    if (n === source) return true;
    if (seen.has(n)) continue;
    seen.add(n);
    for (const e of getChildren(g, n)) if (!e.retired) stack.push(e.target);
  }
  return false;
}

const hasDirectSupport = (ev: EvidenceDraft[]) => ev.some((e) => e.strength === "direct" && e.polarity === "supports");

export function applyPatch(g: Graph, patch: GraphPatch, turn: number, cfg: AgentConfig): ApplyResult {
  const idMap: Record<string, string> = {};
  const rejected: ApplyResult["rejected"] = [];
  const touched = new Set<string>();
  const before = new Map<string, { c: number; s: Status }>();
  const remember = (id: string) => {
    const x = g.nodes[id] ?? g.edges[id];
    if (x && !before.has(id)) before.set(id, { c: x.confidence, s: x.status });
  };
  const touch = (id: string) => { remember(id); touched.add(id); };
  const resolve = (id: string) => idMap[id] ?? id;
  const addEv = (target: { evidence: Evidence[] }, ev: EvidenceDraft) => target.evidence.push({ ...ev, id: nextId(g, "V") });

  // Attempts first, so each records the confidence it started from.
  for (const id of patch.incrementAttempts ?? []) {
    const x = g.nodes[id] ?? g.edges[id];
    if (!x) continue;
    touch(id);
    x.attemptConf.push(x.confidence);
    x.strengthenAttempts++;
    x.updatedTurn = turn;
  }
  for (const id of patch.incrementExpand ?? []) if (g.nodes[id]) { g.nodes[id].expandAttempts++; g.nodes[id].updatedTurn = turn; }
  for (const id of patch.incrementForks ?? []) if (g.nodes[id]) { g.nodes[id].forkCount++; g.nodes[id].updatedTurn = turn; }
  for (const id of patch.incrementBranch ?? []) if (g.nodes[id]) { g.nodes[id].branchAttempts = (g.nodes[id].branchAttempts ?? 0) + 1; g.nodes[id].updatedTurn = turn; g.branchedTurn = turn; }
  for (const id of patch.incrementConnect ?? []) if (g.nodes[id]) g.nodes[id].connectAttempts = (g.nodes[id].connectAttempts ?? 0) + 1;
  if (patch.setConclusion) g.conclusion = patch.setConclusion;
  if (patch.setTheories) {
    g.storiesSince ??= turn;
    g.theorizeCount = (g.theorizeCount ?? 0) + 1;
    g.theorizedTurn = turn;
    if (patch.setTheories.length) {
      for (const t of g.theories ?? []) t.status = "replaced";
      (g.theories ??= []).push(...patch.setTheories);
    }
  }
  if (patch.addProbe) {
    let t = (g.theories ?? []).find((x) => x.id === "PROBES" && x.status === "live");
    if (!t) { t = { id: "PROBES", round: g.theorizeCount ?? 0, story: "Rare records read on their own (not a story).", accountsFor: [], gaps: "", predictions: [], status: "live", support: 0, against: 0 }; (g.theories ??= []).push(t); }
    t.predictions.push(patch.addProbe);
  }
  if (patch.predictionResult) {
    const r = patch.predictionResult;
    const p = (g.theories ?? []).flatMap((t) => t.predictions).find((x) => x.id === r.id);
    if (p) Object.assign(p, { status: r.status, result: r.result, toolCallIds: r.toolCallIds, testedTurn: turn });
    for (const t of g.theories ?? []) { if (r.supports.includes(t.id)) t.support++; if (r.weakens.includes(t.id)) t.against++; }
  }

  for (const n of patch.addNodes) {
    // A proposed cause is never the event it is proposed to cause (an assume-role one second before its own effect
    // shared its key and role and was merged into it, leaving the chain without its link).
    const causes = new Set(patch.addEdges.filter((e) => e.source === n.tempId && e.kind === "explains").map((e) => e.target));
    const match = Object.values(g.nodes).find((m) => !causes.has(m.id) && sameThing(n, m));
    if (match) {
      if (match.status === "ruled-out" && !hasDirectSupport(n.evidence)) {
        rejected.push({ item: n, reason: `matches ruled-out ${match.id} and brings no new direct evidence` });
        continue;
      }
      idMap[n.tempId] = match.id;
      touch(match.id);
      for (const ev of n.evidence) addEv(match, ev);
      for (const a of n.artifacts) if (!match.artifacts.some((b) => sameArtifact(a, b))) match.artifacts.push(a);
      if (!match.observedAt && n.observedAt) match.observedAt = n.observedAt;
      match.updatedTurn = turn;
      continue;
    }
    const id = nextId(g, PREFIX[n.kind]);
    idMap[n.tempId] = id;
    const isMarker = n.kind === "boundary-marker";
    const prior = n.isSymptom ? 0.5 : clampPrior(n.prior, cfg);
    g.nodes[id] = {
      id, kind: n.kind, label: n.label, domain: n.domain, prior,
      status: isMarker ? "unknown" : "suspected", confidence: prior,
      evidence: [], artifacts: [...n.artifacts], observedAt: n.observedAt,
      ...(n.isSymptom ? { isSymptom: true } : {}),
      ...(n.isRoot && !n.legitimate ? { isRoot: true, rootJustification: n.rootJustification } : {}),
      ...(n.deadEnd ? { deadEnd: true } : {}),
      ...(n.legitimate ? { legitimate: true } : {}),
      ...(n.unverified ? { unverified: true } : {}),
      ...(n.takeover ? { takeover: true } : {}),
      ...(isMarker ? { targetDomain: n.targetDomain, originNodeId: n.originNodeId, reason: n.reason, forwardLeaves: [], pivotSteps: 0 } : {}),
      strengthenAttempts: 0, attemptConf: [], expandAttempts: 0, forkCount: 0, branchAttempts: 0,
      confidenceHistory: [], createdTurn: turn, updatedTurn: turn,
    };
    for (const ev of n.evidence) addEv(g.nodes[id], ev);
    touch(id);
  }

  const addEdge = (e: EdgeIn): string | undefined => {
    const source = resolve(e.source), target = resolve(e.target);
    if (!g.nodes[source] || !g.nodes[target]) { rejected.push({ item: e, reason: `unknown endpoint ${!g.nodes[source] ? e.source : e.target}` }); return; }
    if (e.kind === "explains" && makesCycle(g, source, target)) { rejected.push({ item: e, reason: `cycle: ${source} -> ${target}` }); return; }
    const existing = Object.values(g.edges).find((x) => x.kind === e.kind && x.source === source && x.target === target && !x.retired);
    if (existing) {
      if (existing.status === "ruled-out" && !hasDirectSupport(e.evidence)) { rejected.push({ item: e, reason: `edge ${existing.id} is ruled out and no new direct evidence` }); return existing.id; }
      for (const ev of e.evidence) addEv(existing, ev);
      if (e.stitchFor && !existing.stitchFor) existing.stitchFor = e.stitchFor;
      if (e.joint) existing.joint = true;
    if (existing.inferred && !e.inferred && hasDirectSupport(e.evidence)) existing.inferred = false;
      existing.updatedTurn = turn;
      touch(existing.id);
      return existing.id;
    }
    const id = nextId(g, "E");
    const prior = clampPrior(e.prior, cfg);
    g.edges[id] = {
      id, kind: e.kind, source, target, prior, confidence: prior, status: "suspected", disputed: false,
      ...(e.stitchFor ? { stitchFor: e.stitchFor } : {}),
      ...(e.joint ? { joint: true } : {}),
      ...(e.inferred ? { inferred: true } : {}),
      evidence: [], strengthenAttempts: 0, attemptConf: [], confidenceHistory: [], createdTurn: turn, updatedTurn: turn,
    };
    for (const ev of e.evidence) addEv(g.edges[id], ev);
    touch(id);
    return id;
  };
  const created: string[] = [];
  for (const e of patch.addEdges) { const before = new Set(Object.keys(g.edges)); const id = addEdge(e); if (id) { idMap[e.tempId] = id; if (!before.has(id)) created.push(id); } }
  // A new link between two events needs a value both carry. Checked after the patch's attributions are in,
  // since whether a host or an account counts depends on who each event ran as.
  const inferUnjoined = (id: string) => {
    const e = g.edges[id];
    if (!cfg.linkNeedsSharedValue || e?.kind !== "explains" || e.createdTurn !== turn || e.evidence.length !== e.evidence.filter((v) => v.turn === turn).length) return;
    const a = g.nodes[e.source], b = g.nodes[e.target];
    if (a?.kind !== "action" || b?.kind !== "action" || sharedValue(g, a, b, true)) return;
    // Kept as an inferred link; its evidence counts as circumstantial (timing, naming), never as a join.
    e.inferred = true;
    for (const v of e.evidence) v.strength = "circumstantial";
  };
  for (const id of created) inferUnjoined(id);
  // Routine work by one account leads to more of its routine work: a step that causes its own account's legitimate
  // step is legitimate too, unless someone else operated it or it is labelled malicious (a developer's
  // routine step from their usual address was once left unmarked upstream of their legitimate one and became the chain top).
  for (let changed = true; changed; ) {
    changed = false;
    for (const e of Object.values(g.edges)) {
      if (e.kind !== "explains" || e.retired || e.status === "ruled-out") continue;
      const a = g.nodes[e.source], b = g.nodes[e.target];
      if (a?.kind !== "action" || !b?.legitimate || a.legitimate || a.isSymptom || ACCUSE_WORDS.test(a.label)) continue;
      const ta = topAccount(g, a);
      if (!ta || ta !== topAccount(g, b) || getParents(g, a.id, "operated-by").some((x) => !x.retired && x.status !== "ruled-out")) continue;
      a.legitimate = true; a.isRoot = false; a.updatedTurn = turn; touch(a.id); changed = true;
    }
  }

  for (const { targetId, evidence } of patch.addEvidence) {
    const id = resolve(targetId);
    const x = g.nodes[id] ?? g.edges[id];
    if (!x) { rejected.push({ item: { targetId, evidence }, reason: `unknown target ${targetId}` }); continue; }
    addEv(x, evidence);
    x.updatedTurn = turn;
    touch(id);
  }

  const resolvedMarkers: NodeId[] = [];
  const resolveMarker = (markerId: NodeId, nodeId: NodeId) => {
    const m = g.nodes[markerId];
    if (!m || m.kind !== "boundary-marker" || m.resolvedTo) return;
    m.resolvedTo = nodeId;
    m.updatedTurn = turn;
    for (const e of getChildren(g, markerId)) e.retired = true;
    resolvedMarkers.push(markerId);
    touch(markerId);
  };
  if (patch.resolveBoundary) {
    const { markerId, nodeId, joinEvidence } = patch.resolveBoundary;
    const m = g.nodes[markerId];
    const cand = resolve(nodeId);
    if (!m?.originNodeId || !g.nodes[cand]) rejected.push({ item: patch.resolveBoundary, reason: "unknown marker or candidate" });
    else {
      const had = new Set(Object.keys(g.edges));
      const join = addEdge({ tempId: `join:${markerId}`, kind: "explains", source: cand, target: m.originNodeId, prior: 0.3, evidence: joinEvidence });
      if (join) {
        if (!had.has(join)) inferUnjoined(join);
        idMap[`join:${markerId}`] = join;
        resolveMarker(markerId, cand);
      }
    }
  }

  for (const f of patch.setFlags ?? []) {
    const n = g.nodes[resolve(f.nodeId)];
    if (!n) continue;
    if (f.isRoot !== undefined) n.isRoot = f.isRoot;
    if (f.rootJustification) n.rootJustification = f.rootJustification;
    if (f.legitimate !== undefined) n.legitimate = f.legitimate;
    if (n.legitimate) n.isRoot = false; // the attack did not start with routine work
    n.updatedTurn = turn;
    touch(n.id);
  }

  for (const { nodeId, leads } of patch.setLeads ?? []) {
    const n = g.nodes[resolve(nodeId)];
    if (n) { n.leads = leads; n.swept = true; n.updatedTurn = turn; }
  }

  for (const { nodeId, artifacts } of patch.addArtifacts ?? []) {
    const n = g.nodes[resolve(nodeId)];
    if (!n) continue;
    for (const a of artifacts) if (!n.artifacts.some((b) => sameArtifact(a, b))) n.artifacts.push(a);
  }

  for (const u of patch.updateMarkers ?? []) {
    const m = g.nodes[u.markerId];
    if (!m || m.kind !== "boundary-marker") continue;
    if (u.forwardLeaves) m.forwardLeaves = [...new Set(u.forwardLeaves.map(resolve).filter((id) => g.nodes[id]))];
    if (u.pivotSteps !== undefined) m.pivotSteps = u.pivotSteps;
    m.updatedTurn = turn;
    touch(m.id);
  }

  rescore(g, [...touched], turn, cfg);

  // A tentative stitch edge that is now confirmed resolves its marker.
  for (const e of Object.values(g.edges)) if (e.stitchFor && e.status === "confirmed") resolveMarker(e.stitchFor, e.source);

  const changes: Change[] = [];
  for (const id of touched) {
    const x = g.nodes[id] ?? g.edges[id];
    const b = before.get(id);
    if (!x) continue;
    if (!b || Math.abs(b.c - x.confidence) > 1e-9 || b.s !== x.status) changes.push({ id, from: b?.c ?? x.prior, to: x.confidence, status: x.status, prevStatus: b?.s ?? "suspected" });
  }
  return { idMap, rejected, touched: [...touched], changes, resolvedMarkers };
}

function rescore(g: Graph, ids: string[], turn: number, cfg: AgentConfig) {
  for (const id of ids) {
    const x = g.nodes[id] ?? g.edges[id];
    if (!x) continue;
    const c = confidence(x.prior, x.evidence, cfg);
    const isMarker = "kind" in x && x.kind === "boundary-marker";
    const s = deriveStatus(c, x.evidence, cfg, isMarker);
    if (x.confidenceHistory.at(-1)?.value !== c) x.confidenceHistory.push({ turn, value: c });
    x.confidence = c;
    x.status = s;
  }
  // Competing edges into the same target, of the same kind, dispute each other.
  const groups = new Map<string, GraphEdge[]>();
  for (const e of Object.values(g.edges)) {
    if (e.retired) continue;
    const k = `${e.kind}|${e.target}`;
    (groups.get(k) ?? groups.set(k, []).get(k)!).push(e);
  }
  // Joint causes are needed together, so they do not dispute each other; nor do linked identities.
  for (const es of groups.values()) {
    // A carrier service is neither a rival nor a link between rivals.
    const live = es.filter((e) => e.status !== "ruled-out" && g.nodes[e.source]?.status !== "ruled-out" && !e.joint && !(e.kind !== "explains" && isCarrier(g.nodes[e.source]!)));
    // Identities are grouped into chains (linked directly or through another identity on the same action).
    const chain = new Map<string, number>();
    if (es[0]?.kind !== "explains") {
      const ids = live.map((e) => e.source);
      ids.forEach((id, i) => chain.set(id, i));
      for (let changed = true; changed; ) {
        changed = false;
        for (const x of ids) for (const y of ids) if (chain.get(x) !== chain.get(y) && linkedPrincipals(g.nodes[x]!, g.nodes[y]!)) {
          const to = Math.min(chain.get(x)!, chain.get(y)!), from = Math.max(chain.get(x)!, chain.get(y)!);
          for (const [k, v] of chain) if (v === from) chain.set(k, to);
          changed = true;
        }
      }
    }
    for (const e of es) e.disputed = live.includes(e) && live.some((o) => o !== e && (e.kind === "explains" || chain.get(o.source) !== chain.get(e.source)));
  }
}

// Identities on one action that belong to one chain are not rivals: a role and its assumed session, a function
// and the session named after it share a distinctive name; a cloud service that only carries the call
// (lambda.amazonaws.com, aws:service:...) competes with no one.
const GENERIC = new Set(["aws", "arn", "sts", "iam", "role", "assumed-role", "function", "service", "lambda", "amazonaws", "com", "user", "users", "system", "serviceaccount", "k8s", "ad", "github", "account"]);
function principalTokens(n: GraphNode): Set<string> {
  const out = new Set<string>();
  for (const a of n.artifacts.length ? n.artifacts : [{ kind: "principal", value: n.label }])
    for (const t of a.value.toLowerCase().split(/[:/@\\\s]+/)) if (t.length >= 4 && !GENERIC.has(t) && !/^\d+$/.test(t) && !/^[a-z]{2}-[a-z]+-\d$/.test(t)) out.add(t);
  return out;
}
const isCarrier = (n: GraphNode) => n.artifacts.concat([{ kind: "principal", value: n.label }]).some((a) => /\.amazonaws\.com$|^aws:service:|\.googleapis\.com$/i.test(a.value.trim()));
export function linkedPrincipals(a: GraphNode, b: GraphNode): boolean {
  const ta = principalTokens(a);
  return [...principalTokens(b)].some((t) => ta.has(t));
}

// A link joins two events on a value both carry: one event's process, session, container, token,
// key, file, address, account... appears in the other's artifacts or label. A host joins them only inside one source
// for one account, and an account only when both ran as it: same host and nearby time is how unrelated work gets
// chained in (an attacker's logon to a server, then an admin's remote job there a minute later; a pod created, then
// a container start on that node seconds later, across sources; both blamed a legitimate user).
const LOOSE_KINDS = new Set(["host", "user"]);
const ACCUSE_WORDS = /\b(attacker|malicious|stolen|compromised|hijacked|backdoor|exfiltrat\w*|rogue|implant)\b/i;
function topAccount(g: Graph, n: GraphNode): string | undefined {
  return getParents(g, n.id, "attributed-to").filter((e) => !e.retired && e.status !== "ruled-out").sort((a, b) => b.confidence - a.confidence)[0]?.source;
}
// A role or user ARN names an account, like a user name: it joins two events only when both ran as it (a
// developer's change and an attacker's function once shared one admin role through different sessions).
// Across different accounts it joins them only as a hand-off: the cause (a) carries the role as a resource and the
// effect (b) runs as it (a function created with an execution role, then running as it; a token, then its session).
const principalArn = (x: Artifact) => x.kind === "arn" && /:(role|user|assumed-role)\//i.test(x.value);
let roleArnRule = true; // set per call; off reproduces the rule before role hand-offs
const accountLike = (x: Artifact) => LOOSE_KINDS.has(x.kind) || (roleArnRule && principalArn(x));
const roleKey = (v: string) => { const m = v.match(/:(?:role|assumed-role)\/([^/]+)/i) ?? v.match(/:user\/(.+)$/i); return m ? m[1]!.toLowerCase() : ""; };
export function sharedValue(g: Graph, a: GraphNode, b: GraphNode, roleArns = true): string | null {
  roleArnRule = roleArns;
  const ta = topAccount(g, a), tb = topAccount(g, b);
  const sameAccount = !ta || !tb || ta === tb;
  const ranUnder = (actor: string | undefined, v: string) => { const k = roleKey(v), l = (actor && g.nodes[actor]?.label.toLowerCase()) || ""; return !!k && (l.includes(`role/${k}`) || l.includes(`user/${k}`)); };
  const looseOk = (x: Artifact) => (x.kind === "host" ? sameAccount && a.domain === b.domain : roleArnRule && principalArn(x) ? sameAccount || (ranUnder(tb, x.value) && !ranUnder(ta, x.value)) : accountLike(x) ? sameAccount : true);
  // Host and account values that do not count here, in any spelling that contains them.
  const off = [...a.artifacts, ...b.artifacts].filter((x) => accountLike(x) && !looseOk(x)).flatMap((x) => { const v = x.value.trim().toLowerCase(); return [v, v.split(/[.@\\]/)[0]!]; }).filter((v) => v.length >= 3);
  const blocked = (t: string) => off.some((o) => t.includes(o));
  const text = (n: GraphNode) => [n.label, ...n.artifacts.map((x) => x.value)].join("\n").toLowerCase();
  const ta2 = text(a), tb2 = text(b);
  // 1. An artifact value of one in the other (exact, contained, or a path's file name).
  for (const [x, y, other] of [[a, b, tb2], [b, a, ta2]] as const)
    for (const art of x.artifacts) {
      const v = art.value.trim().toLowerCase();
      if (v.length < 3 || blocked(v) || (accountLike(art) && !looseOk(art))) continue;
      const base = v.split(/[\\/]/).at(-1)!;
      if (y.artifacts.some((z) => z.value.trim().toLowerCase() === v) || (v.length >= 5 && other.includes(v)) || (base !== v && base.length >= 5 && other.includes(base))) return `${art.kind}=${art.value}`;
    }
  // 2. An identifier-like token in both (a file name, id, address: has a digit, 6+ chars, not a date or time).
  // Parts of an id count too: the same invoice number in "INV-10001", "<10001.invoice@...>" and "Invoice_10001.xlsm"
  // (an email -> attachment-open link was once refused for want of it).
  // Not an ARN prefix ("arn:aws:sts::<account>:assumed-role") or a 12-digit cloud account id: every event in the
  // account carries them (a developer's change once matched an attacker's session on them).
  const ok = (x: string) => /\d/.test(x) && !/^\d{4}-\d\d-\d\d|^\d\d:\d\d/.test(x) && !/^\d{4}$/.test(x) && !(roleArnRule && (/^\d{12}$/.test(x) || x.startsWith("arn:"))) && !blocked(x);
  const toks = (t: string) => new Set((t.match(/[a-z0-9][\w.@:-]{4,}[a-z0-9]/g) ?? []).flatMap((x) => [x, ...x.split(/[._@:-]/).filter((y) => y.length >= 5)]).filter(ok));
  const tb3 = toks(tb2);
  for (const t of toks(ta2)) if (tb3.has(t)) return `token ${t}`;
  return null;
}

// Paths walk upward from the symptom along explains edges, skipping ruled-out nodes and edges, retired
// edges and resolved markers. Score = product of node and edge confidences; a marker counts its edge only.
function liveParents(g: Graph, nodeId: NodeId): GraphEdge[] {
  return getParents(g, nodeId).filter((e) => {
    const s = g.nodes[e.source];
    return !e.retired && e.status !== "ruled-out" && s && s.status !== "ruled-out" && !(s.kind === "boundary-marker" && s.resolvedTo);
  });
}
// Findings not linked to the alert: live, plausible (>= 0.5) attack events with no chain of live links to the
// symptom, upstream or as an effect of a chain event. Tests turn up events with no link shown (findings a test turned up
// once all stayed unlinked, and the report's chain stopped at an open boundary). Latest first: those closest to the chain go first.
// minLink: only links at least this strong count (the report's indicators use 0.5).
export function islands(g: Graph, minLink = 0): GraphNode[] {
  const s = symptomOf(g);
  if (!s) return [];
  const reach = new Set<NodeId>([s.id]);
  for (const todo = [s.id]; todo.length; ) for (const e of liveParents(g, todo.pop()!)) if (!reach.has(e.source) && e.confidence >= minLink) { reach.add(e.source); todo.push(e.source); }
  // An effect of a chain event (a branch finding: the credential theft the implant ran) is linked too, just not upstream.
  const live = (e: GraphEdge) => e.kind === "explains" && !e.retired && e.status !== "ruled-out" && e.confidence >= minLink && g.nodes[e.target]?.status !== "ruled-out";
  for (const todo = [...reach]; todo.length; ) { const id = todo.pop()!; for (const e of Object.values(g.edges)) if (e.source === id && live(e) && !reach.has(e.target)) { reach.add(e.target); todo.push(e.target); } }
  return Object.values(g.nodes).filter((n) => n.kind === "action" && !reach.has(n.id) && !n.isSymptom && !n.legitimate && n.status !== "ruled-out" && n.confidence >= 0.5)
    .sort((a, b) => (b.observedAt ?? "").localeCompare(a.observedAt ?? "") || a.createdTurn - b.createdTurn);
}

const nodeScore = (n: GraphNode) => (n.kind === "boundary-marker" ? 1 : n.confidence);

export const symptomOf = (g: Graph) => Object.values(g.nodes).find((n) => n.isSymptom);

export function allPaths(g: Graph, limit = 50): Path[] {
  const s = symptomOf(g);
  if (!s) return [];
  const out: Path[] = [];
  const walk = (nodeIds: NodeId[], edgeIds: EdgeId[], score: number) => {
    if (out.length >= 1000) return;
    const top = nodeIds[nodeIds.length - 1]!;
    const parents = nodeIds.length >= MAX_DEPTH ? [] : liveParents(g, top).filter((e) => !nodeIds.includes(e.source));
    if (!parents.length) { out.push({ nodeIds, edgeIds, score }); return; }
    for (const e of parents) walk([...nodeIds, e.source], [...edgeIds, e.id], score * e.confidence * nodeScore(g.nodes[e.source]!));
  };
  walk([s.id], [], nodeScore(s));
  // More solid hops first (a link >= 0.5 into an event >= 0.6), then score. A product score alone always favours
  // the shortest path, so a chain that stops at an open boundary after one hop beat the real three-hop chain to the
  // credential (a verdict once stopped at a boundary two hops up, without the step that minted the credential).
  const solid = (p: Path) => p.edgeIds.filter((id, i) => g.edges[id]!.confidence >= 0.5 && g.nodes[p.nodeIds[i + 1]!]?.kind === "action" && g.nodes[p.nodeIds[i + 1]!]!.confidence >= 0.6).length;
  const hops = new Map(out.map((p) => [p, solid(p)]));
  return out.sort((a, b) => hops.get(b)! - hops.get(a)! || b.score - a.score || a.nodeIds.length - b.nodeIds.length).slice(0, limit);
}

export function bestPathFromSymptom(g: Graph): Path | null {
  return allPaths(g, 1)[0] ?? null;
}
